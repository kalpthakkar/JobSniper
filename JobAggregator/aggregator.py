"""
JobAggregator — Central job accumulator using NATS.

Features:
- Subscribes to NATS for job batches from multiple workers
- Maintains a priority queue of jobs based on ATS score and recency
- Provides REST API endpoint to get the best job to apply
"""

import asyncio
import heapq
import itertools
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import nats
from flask import Flask, jsonify
import logging as flask_logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("job_aggregator")

# Suppress Flask logging
flask_logging.getLogger('werkzeug').setLevel(flask_logging.WARNING)

# ATS scoring weights (higher = better)
ATS_SCORES = {
    "greenhouse": 10,
    "lever": 9,
    "ashby": 8,
    "workday": 7,
    "workable": 6,
}

class JobScorer:
    """Scores jobs based on ATS type and recency."""
    
    def __init__(self):
        self.base_time = time.time()
    
    def score_job(self, job: Dict[str, Any]) -> float:
        """Calculate priority score for a job."""
        # ATS score
        ats_score = ATS_SCORES.get(job.get("company", "").lower(), 5)  # Default score
        
        # Recency score (newer jobs get higher score)
        posted_at = job.get("posted_at")
        if posted_at:
            try:
                posted_time = datetime.fromisoformat(posted_at.replace('Z', '+00:00')).timestamp()
            except:
                posted_time = time.time()
        else:
            posted_time = time.time()
        
        # Recency factor: exponential decay over time (half-life ~1 day = 86400 seconds)
        age_hours = (time.time() - posted_time) / 3600
        recency_score = max(0, 10 * (0.5 ** (age_hours / 24)))
        
        # Total score: ATS weight + recency
        total_score = ats_score + recency_score
        
        return total_score

class JobAggregator:
    """Central job accumulator with priority queue."""
    
    def __init__(self, nats_servers: List[str], subject: str = "job_sniper.jobs", max_queue_size: int = 100):
        self.nats_servers = nats_servers
        self.subject = subject
        self.max_queue_size = max_queue_size
        self.scorer = JobScorer()
        
        # Priority queue: (score, job_id, job_data)
        self.job_queue = []
        self.job_map = {}  # job_id -> job_data
        self.lock = asyncio.Lock()
        self._counter = itertools.count()
        
        self.nc: Optional[nats.NATS] = None
        self.running = False
        self.loop = None
    
    async def start(self):
        """Start the aggregator."""
        self.running = True
        self.loop = asyncio.get_event_loop()
        
        # Connect to NATS
        try:
            self.nc = await nats.connect(self.nats_servers)
            logger.info(f"Connected to NATS servers: {self.nats_servers}")
        except Exception as e:
            logger.error(f"Failed to connect to NATS: {e}")
            return
        
        # Subscribe to job batches
        await self.nc.subscribe(self.subject, cb=self._on_job_batch)
        logger.info(f"Subscribed to subject: {self.subject}")
        
        # Start Flask app in background thread
        flask_thread = threading.Thread(target=self._run_flask, daemon=True)
        flask_thread.start()
    
    async def stop(self):
        """Stop the aggregator."""
        self.running = False
        if self.nc:
            await self.nc.close()
    
    async def _on_job_batch(self, msg):
        """Handle incoming job batch from NATS."""
        try:
            data = json.loads(msg.data.decode())
            jobs = data.get("jobs", [])
            
            logger.info(f"Received batch of {len(jobs)} jobs")
            
            async with self.lock:
                for job in jobs:
                    job_id = job.get("id")
                    if job_id is None:
                        logger.warning("[job_aggregator] Skipping job with missing id")
                        continue
                    
                    score = self.scorer.score_job(job)
                    counter = next(self._counter)
                    
                    # Add to priority queue with unique tie-breaker
                    heapq.heappush(self.job_queue, (-score, counter, job_id, job))
                    self.job_map[job_id] = job
                    
                    # Maintain queue size with stale-entry cleanup
                    while len(self.job_queue) > self.max_queue_size:
                        _, _, removed_id, removed_job = heapq.heappop(self.job_queue)
                        current_job = self.job_map.get(removed_id)
                        if current_job is removed_job or current_job == removed_job:
                            self.job_map.pop(removed_id, None)
            
            logger.debug(f"Queue now has {len(self.job_queue)} jobs")
        
        except Exception as e:
            logger.error(f"Error processing job batch: {e}")
    
    async def get_best_job(self) -> Optional[Dict[str, Any]]:
        """Get the highest priority job."""
        async with self.lock:
            while self.job_queue:
                _, _, job_id, job = self.job_queue[0]
                current_job = self.job_map.get(job_id)
                if current_job is None or current_job != job:
                    heapq.heappop(self.job_queue)
                    continue
                return job
            return None
    
    def _run_flask(self):
        """Run Flask API server."""
        try:
            from werkzeug.serving import make_server
            
            app = Flask(__name__)
            app.logger.setLevel(flask_logging.WARNING)
            
            # Store self reference for route handlers
            aggregator_ref = self
            
            @app.route('/', methods=['GET'])
            def health():
                return jsonify({"status": "ok", "message": "JobAggregator API running"})
            
            @app.route('/best-job', methods=['GET'])
            def best_job():
                # Run async function in the event loop
                try:
                    future = asyncio.run_coroutine_threadsafe(aggregator_ref.get_best_job(), aggregator_ref.loop)
                    job = future.result(timeout=5)
                    if job:
                        return jsonify({
                            "status": "success",
                            "job": job
                        })
                    else:
                        return jsonify({
                            "status": "no_jobs",
                            "message": "No jobs available in queue"
                        })
                except Exception as e:
                    logger.error(f"Error in best_job: {e}")
                    return jsonify({"status": "error", "message": str(e)}), 500
            
            @app.route('/queue-stats', methods=['GET'])
            def queue_stats():
                # For stats, we can access synchronously since it's just reading length
                try:
                    return jsonify({
                        "queue_size": len(aggregator_ref.job_queue),
                        "max_size": aggregator_ref.max_queue_size
                    })
                except Exception as e:
                    logger.error(f"Error in queue_stats: {e}")
                    return jsonify({"status": "error", "message": str(e)}), 500
            
            logger.info("Creating Flask server on 0.0.0.0:5001")
            server = make_server('0.0.0.0', 5001, app, threaded=True)
            logger.info("Starting Flask server...")
            server.serve_forever()
            
        except Exception as e:
            logger.error(f"Fatal error in Flask thread: {e}", exc_info=True)

async def main():
    # Configuration
    NATS_SERVERS = ["nats://localhost:4222"]  # Can be LAN IP, e.g., "nats://192.168.1.100:4222"
    SUBJECT = "job_sniper.jobs"
    
    aggregator = JobAggregator(NATS_SERVERS, SUBJECT)
    
    try:
        await aggregator.start()
        logger.info("JobAggregator started. Press Ctrl+C to stop.")
        
        # Keep running
        while aggregator.running:
            await asyncio.sleep(1)
    
    except KeyboardInterrupt:
        logger.info("Stopping JobAggregator...")
    finally:
        await aggregator.stop()

if __name__ == "__main__":
    asyncio.run(main())