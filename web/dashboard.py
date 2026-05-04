"""
web/dashboard.py — Web dashboard for managing companies in Job Sniper.
"""
import json
import logging
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify

from core.config import Config
from core.database import JobDatabase
from core.models import ATSType, Priority

logger = logging.getLogger("job_sniper.dashboard")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
config = Config(str(PROJECT_ROOT / "config.yaml"))
app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
    static_folder=str(PROJECT_ROOT / "web" / "static"),
)

db_path = Path(config.db_path)
if not db_path.is_absolute():
    db_path = PROJECT_ROOT / db_path

db = JobDatabase(str(db_path))

@app.route('/')
def index():
    companies = db.get_companies()
    ats_types = [ats.value for ats in ATSType]
    priorities = [p.value for p in Priority]
    return render_template('index.html', companies=companies, ats_types=ats_types, priorities=priorities, live_reload=True)

# Default values for new company form
DEFAULT_ATS = "workday"
DEFAULT_PRIORITY = "MID"

@app.route('/add-company', methods=['GET', 'POST'])
def add_company():
    ats_types = [ats.value for ats in ATSType]
    priorities = [p.value for p in Priority]
    if request.method == 'POST':
        board_token = request.form.get('board_token', '').strip()
        ats = request.form.get('ats', '').strip()
        priority = request.form.get('priority', '').strip()
        
        # If AJAX request, return JSON
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            if not board_token or not ats or not priority:
                return {"status": "error", "message": "All fields are required"}, 400
            try:
                db.add_company(board_token, ats, priority)
                logger.info(f"[ADD_COMPANY] Added {board_token} ({ats}, {priority})")
                return {"status": "success", "message": f"Company added: {board_token}"}
            except Exception as e:
                logger.error(f"[ADD_COMPANY] Error adding company: {e}")
                return {"status": "error", "message": f"Error adding company: {str(e)}"}, 400
        
        # Regular form submission (for backwards compatibility)
        if board_token and ats and priority:
            db.add_company(board_token, ats, priority)
        return redirect(url_for('index'))

    return render_template(
        'add_company.html',
        ats_types=ats_types,
        priorities=priorities,
        default_ats=DEFAULT_ATS,
        default_priority=DEFAULT_PRIORITY,
    )

@app.route('/notify', methods=['GET', 'POST'])
def notify_settings():
    config_value = db.get_notification_config() or {}
    default_config = {
        "enabled": False,
        "job_title": {"enabled": False, "rules": []},
        "blacklist_job_title": {"enabled": False, "rules": []},
        "company_name": {"enabled": False, "rules": []},
        "blacklist_company_name": {"enabled": False, "rules": []},
        "location": {"enabled": False, "rules": []},
        "blacklist_location": {"enabled": False, "rules": []},
    }

    if request.method == 'POST':
        # Handle AJAX JSON requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            try:
                data = request.get_json()
                if isinstance(data, dict):
                    # Validate and remove duplicates from each section
                    for section_key in ['job_title', 'blacklist_job_title', 'company_name', 
                                       'blacklist_company_name', 'location', 'blacklist_location']:
                        if section_key in data and isinstance(data[section_key], dict):
                            rules = data[section_key].get('rules', [])
                            if isinstance(rules, list):
                                # Remove duplicates, keeping first occurrence
                                seen = set()
                                unique_rules = []
                                for rule in rules:
                                    if isinstance(rule, dict):
                                        rule_key = (rule.get('value', ''), rule.get('match', ''), rule.get('case_sensitive', False))
                                        if rule_key not in seen and rule_key[0]:  # Skip empty values
                                            seen.add(rule_key)
                                            unique_rules.append(rule)
                                data[section_key]['rules'] = unique_rules
                    
                    db.save_notification_config(data)
                    return jsonify({"status": "success", "message": "Rules saved successfully (duplicates removed)"})
                else:
                    return jsonify({"status": "error", "message": "Invalid configuration format"}), 400
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 400
        
        # Handle traditional form submissions
        raw_config = request.form.get('notification_config', '{}')
        try:
            parsed = json.loads(raw_config)
            if isinstance(parsed, dict):
                db.save_notification_config(parsed)
        except Exception:
            pass
        return redirect(url_for('notify_settings'))

    final_config = {
        "enabled": config_value.get("enabled", default_config["enabled"]),
        "job_title": {**default_config["job_title"], **config_value.get("job_title", {})},
        "blacklist_job_title": {**default_config["blacklist_job_title"], **config_value.get("blacklist_job_title", {})},
        "company_name": {**default_config["company_name"], **config_value.get("company_name", {})},
        "blacklist_company_name": {**default_config["blacklist_company_name"], **config_value.get("blacklist_company_name", {})},
        "location": {**default_config["location"], **config_value.get("location", {})},
        "blacklist_location": {**default_config["blacklist_location"], **config_value.get("blacklist_location", {})},
    }
    return render_template('notify.html', config=final_config)

@app.route("/api/notification/channels", methods=["GET"])
def get_notification_channels():
    """Get current notification channel settings."""
    channels = {
        "console": db.get_setting("notify_channel_console") != "false",
        "telegram": db.get_setting("notify_channel_telegram") != "false",
        "webhook": db.get_setting("notify_channel_webhook") != "false",
    }
    return {"channels": channels, "status": "ok"}

@app.route("/api/notification/channels", methods=["POST"])
def toggle_notification_channel():
    """Toggle a notification channel on/off."""
    data = request.get_json()
    channel = data.get("channel", "").lower()
    enabled = data.get("enabled", True)
    
    if channel not in ["console", "telegram", "webhook"]:
        return {"status": "error", "message": "Invalid channel"}, 400
    
    key = f"notify_channel_{channel}"
    value = "true" if enabled else "false"
    db.set_setting(key, value)
    saved_value = db.get_setting(key)
    logger.info(f"[NOTIFICATION] {channel} = {value} (verified: {saved_value})")
    return {"status": "ok", "channel": channel, "saved": value, "verified": saved_value}

@app.route('/update/<path:board_token>', methods=['POST'])
def update(board_token):
    ats = request.form['ats']
    priority = request.form['priority']
    
    # Check if this is an AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            db.update_company(board_token, ats, priority)
            logger.info(f"[UPDATE_COMPANY] {board_token} → ATS:{ats}, Priority:{priority}")
            return {"status": "success", "message": f"Updated {board_token}"}
        except Exception as e:
            logger.error(f"[UPDATE_COMPANY] Error updating {board_token}: {e}")
            return {"status": "error", "message": str(e)}, 400
    
    # Regular form submission (backward compatibility)
    db.update_company(board_token, ats, priority)
    return redirect(url_for('index'))

@app.route('/delete/<path:board_token>', methods=['POST'])
def delete(board_token):
    # Check if this is an AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            db.delete_company(board_token)
            logger.info(f"[DELETE_COMPANY] Deleted {board_token}")
            return {"status": "success", "message": f"Deleted {board_token}"}
        except Exception as e:
            logger.error(f"[DELETE_COMPANY] Error deleting {board_token}: {e}")
            return {"status": "error", "message": str(e)}, 400
    
    # Regular form submission (backward compatibility)
    db.delete_company(board_token)
    return redirect(url_for('index'))

@app.route('/preferences', methods=['GET', 'POST'])
def preferences():
    """User job preferences page (clearance, salary range, etc)."""
    if request.method == 'GET':
        # Check if JSON format requested (for AJAX data loading)
        if request.args.get('format') == 'json':
            prefs = db.get_preferences()
            return {"preferences": prefs}
        
        # Return HTML page
        return render_template('preferences.html', current_preferences=db.get_preferences())
    
    if request.method == 'POST':
        # Check if AJAX request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            try:
                data = request.get_json()
                if not data:
                    return {"status": "error", "message": "No data provided"}, 400
                
                db.save_preferences(data)
                logger.info(f"[PREFERENCES] Saved: {data}")
                return {"status": "success", "message": "Preferences saved"}
            except Exception as e:
                logger.error(f"[PREFERENCES] Error saving: {e}")
                return {"status": "error", "message": str(e)}, 400
        
        # Regular form submission
        data = request.form.to_dict()
        db.save_preferences(data)
        return redirect(url_for('preferences'))

@app.route("/settings")
def settings():
    ats_types = [ats.value for ats in ATSType]
    # Load ATS enabled status from database
    ats_enabled = {}
    for ats in ats_types:
        setting = db.get_setting(f"ats_{ats}")
        ats_enabled[ats] = setting != "false"  # Default to True if not set
    
    # Load company-specific enabled status from database
    company_enabled = {}
    for company in ["google", "tesla", "apple", "microsoft"]:
        setting = db.get_setting(f"company_{company}")
        if setting is None:
            # Initialize to "true" if never set
            db.set_setting(f"company_{company}", "true")
            company_enabled[company] = True
        else:
            company_enabled[company] = setting != "false"  # Default to True if not set
    
    return render_template(
        "settings.html", 
        ats_types=ats_types, 
        ats_enabled=ats_enabled,
        company_enabled=company_enabled
    )

@app.route("/api/settings/ats/<ats_type>", methods=["POST"])
def toggle_ats(ats_type):
    data = request.get_json()
    enabled = data.get("enabled", True)
    key = f"ats_{ats_type}"
    value = "true" if enabled else "false"
    db.set_setting(key, value)
    # Verify it was saved
    saved_value = db.get_setting(key)
    logger.info(f"[SETTINGS] ats_{ats_type} = {value} (verified: {saved_value})")
    return {"status": "ok", "saved": value, "verified": saved_value}

@app.route("/api/settings/company/<company_key>", methods=["POST"])
def toggle_company(company_key):
    data = request.get_json()
    enabled = data.get("enabled", True)
    if company_key in ["google", "tesla", "apple", "microsoft"]:
        key = f"company_{company_key}"
        value = "true" if enabled else "false"
        db.set_setting(key, value)
        saved_value = db.get_setting(key)
        logger.info(f"[SETTINGS] company_{company_key} = {value} (verified: {saved_value})")
        return {"status": "ok", "saved": value, "verified": saved_value}
    else:
        # Individual company
        db.update_company_enabled(company_key, enabled)
        logger.info(f"[SETTINGS] company {company_key} enabled = {enabled}")
        return {"status": "ok"}

@app.route("/api/debug/settings", methods=["GET"])
def debug_settings():
    """Debug endpoint to check what settings are stored in the database."""
    from core.models import ATSType
    
    settings = {}
    
    # Get all ATS settings
    for ats in ATSType:
        key = f"ats_{ats.value}"
        value = db.get_setting(key)
        settings[key] = value
    
    # Get company settings
    for company in ["google", "tesla", "apple", "microsoft"]:
        key = f"company_{company}"
        value = db.get_setting(key)
        # Convert to boolean for consistency (None/"true"/"false" -> True/False)
        settings[key] = value != "false"  # Default to True if not set or set to "true"
    
    logger.info(f"[DEBUG] Current settings: {settings}")
    return {"settings": settings, "status": "ok"}

@app.route("/stats")
def stats():
    """Display statistics about each ATS adapter."""
    from collections import defaultdict
    
    companies = db.get_companies()
    ats_types = [ats.value for ats in ATSType]
    
    # Build statistics for each ATS
    ats_stats = []
    for ats in ats_types:
        high_count = 0
        mid_count = 0
        low_count = 0
        total_count = 0
        
        for company in companies:
            if company.ats.value == ats and company.enabled:
                total_count += 1
                if company.priority.value == "HIGH":
                    high_count += 1
                elif company.priority.value == "MID":
                    mid_count += 1
                elif company.priority.value == "LOW":
                    low_count += 1
        
        # Get ATS enabled status
        setting = db.get_setting(f"ats_{ats}")
        is_enabled = setting != "false"
        
        ats_stats.append({
            'name': ats,
            'is_enabled': is_enabled,
            'total': total_count,
            'high': high_count,
            'mid': mid_count,
            'low': low_count,
            'adapter_type': 'ats',
        })
    
    # Add company-specific adapters (Google, Tesla, Apple, Microsoft)
    # Note: Company-specific adapters are standalone adapters (total=1), not collections
    # of companies, so we don't count individual companies or track priorities.
    for company_name in ["google", "tesla", "apple", "microsoft"]:
        # Get company enabled status
        setting = db.get_setting(f"company_{company_name}")
        is_enabled = setting != "false"
        
        ats_stats.append({
            'name': company_name.title(),
            'is_enabled': is_enabled,
            'total': 1,
            'high': 0,
            'mid': 0,
            'low': 0,
            'adapter_type': 'company',
        })
    
    return render_template('stats.html', ats_stats=ats_stats)

# ─────────────────────────────────────────────────────────────
# NEW: Failure Monitoring & Incident Response (Phase 3)
# ─────────────────────────────────────────────────────────────

@app.route('/failures')
def failures():
    """Failure monitoring dashboard — shows recent failed tokens."""
    return render_template('failed_tokens.html')

@app.route('/api/failed-tokens', methods=['GET'])
def api_get_failed_tokens():
    """Get recent failed tokens (last 100) for monitoring dashboard."""
    try:
        limit = request.args.get('limit', default=100, type=int)
        failures = db.get_recent_failures(limit=limit)
        return jsonify({
            "status": "success",
            "data": failures,
            "count": len(failures)
        })
    except Exception as e:
        logger.error(f"[API] Error getting failed tokens: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/failures/stats', methods=['GET'])
def api_get_failure_stats():
    """Get failure statistics and summary."""
    try:
        stats = db.get_failure_stats()
        return jsonify({
            "status": "success",
            "data": stats
        })
    except Exception as e:
        logger.error(f"[API] Error getting failure stats: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/failed-tokens/delete', methods=['DELETE'])
def api_delete_failed_token():
    """Delete a failed token from the system (incident response)."""
    try:
        data = request.get_json()
        board_token = data.get('board_token')
        ats = data.get('ats')
        
        if not board_token or not ats:
            return jsonify({"status": "error", "message": "board_token and ats required"}), 400
        
        # Delete from companies, jobs, and clear failure record
        db.delete_company(board_token)
        db.clear_failure(board_token, ats)
        
        logger.info(f"[INCIDENT] Deleted token: {board_token} ({ats})")
        return jsonify({
            "status": "success",
            "message": f"Deleted {board_token} from monitoring"
        })
    except Exception as e:
        logger.error(f"[API] Error deleting token: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/failed-tokens/clear-failure', methods=['POST'])
def api_clear_failure():
    """Clear failure record for a token (e.g., after manual fix)."""
    try:
        data = request.get_json()
        board_token = data.get('board_token')
        ats = data.get('ats')
        
        if not board_token or not ats:
            return jsonify({"status": "error", "message": "board_token and ats required"}), 400
        
        # Clear failure record (allows immediate retry)
        db.clear_failure(board_token, ats)
        
        logger.info(f"[INCIDENT] Cleared failure: {board_token} ({ats})")
        return jsonify({
            "status": "success",
            "message": f"Cleared failures for {board_token}"
        })
    except Exception as e:
        logger.error(f"[API] Error clearing failure: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)