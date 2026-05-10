# JobAggregator

Central job accumulator for Job Sniper that collects jobs from multiple polling workers via NATS and maintains a priority queue of the best jobs to apply for.

## Features

- **NATS Integration**: Subscribes to job batches from distributed workers
- **Priority Queue**: Scores jobs based on ATS type and posting recency
- **REST API**: Provides endpoints to retrieve the highest-priority job
- **LAN Support**: Configurable to connect to NATS servers over local network

## Setup

1. **Install NATS Server**:
   ```bash
   # Download and install NATS server
   go install github.com/nats-io/nats-server/v2@latest
   
   # Or download binary from https://github.com/nats-io/nats-server/releases
   ```

2. **Start NATS Server**:
   ```bash
   # For local development
   nats-server
   
   # For LAN access (replace with your IP)
   nats-server -a 192.168.1.100
   ```

3. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the aggregator**:
   ```bash
   python aggregator.py
   ```

## NATS Configuration

The aggregator connects to NATS servers specified in the code. Update `NATS_SERVERS` in `aggregator.py`:

```python
NATS_SERVERS = ["nats://localhost:4222"]  # For local
# Or for LAN:
NATS_SERVERS = ["nats://192.168.1.100:4222"]
```

### Running NATS Server

Install NATS server and run it accessible over LAN:

```bash
# Install NATS
go install github.com/nats-io/nats-server/v2@latest

# Run on localhost only
nats-server

# Run accessible on LAN (replace with your IP)
nats-server -a 192.168.1.100
```

## API Endpoints

- `GET /best-job`: Returns the highest-priority job
- `GET /queue-stats`: Returns queue statistics

## Job Scoring

Jobs are scored based on:
- **ATS Score**: Higher for premium ATS platforms (Greenhouse=10, Lever=9, etc.)
- **Recency**: Exponential decay based on posting age (half-life ~1 day)

The priority queue maintains the top N jobs for quick access.