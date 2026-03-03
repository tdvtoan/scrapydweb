# coding: utf-8
"""
REST API for managing ScrapydWeb timer tasks programmatically.

Endpoints:
  GET  /api/timer/tasks         - List all timer tasks
  POST /api/timer/tasks         - Create a new timer task
  GET  /api/timer/tasks/<id>    - Get task details
  POST /api/timer/tasks/<id>/fire - Fire task immediately
  POST /api/timer/scheduler/enable  - Enable scheduler
  POST /api/timer/scheduler/disable - Disable scheduler
  GET  /api/timer/scheduler/status  - Get scheduler status
"""

import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request

from ...models import Task, db
from ...utils.scheduler import scheduler
from ...vars import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED

bp = Blueprint("timer_api", __name__, url_prefix="/api/timer")
logger = logging.getLogger("scrapydweb.timer_api")

# Simple auth check using same credentials as ScrapydWeb
def check_auth():
    """Check if request has valid auth."""
    # In production, auth is handled by nginx
    # This is a fallback for direct access
    from flask import current_app

    if not current_app.config.get("ENABLE_AUTH", False):
        return True

    auth = request.authorization
    if not auth:
        return False

    username = current_app.config.get("USERNAME")
    password = current_app.config.get("PASSWORD")

    return auth.username == username and auth.password == password


def get_scheduler():
    """Get APScheduler instance."""
    return scheduler


@bp.route("/scheduler/status")
def scheduler_status():
    """Get scheduler status."""
    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({"error": "Scheduler not available"}), 500

    state_map = {STATE_RUNNING: "running", STATE_PAUSED: "paused", STATE_STOPPED: "stopped"}

    return jsonify(
        {
            "status": "ok",
            "scheduler_state": state_map.get(scheduler.state, "unknown"),
            "scheduler_state_code": scheduler.state,
        }
    )


@bp.route("/scheduler/enable", methods=["POST"])
def scheduler_enable():
    """Enable the scheduler."""
    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({"error": "Scheduler not available"}), 500

    if scheduler.state == STATE_PAUSED:
        scheduler.resume()
        return jsonify({"status": "ok", "message": "Scheduler enabled"})
    else:
        return jsonify({"status": "ok", "message": f"Scheduler already running (state: {scheduler.state})"})


@bp.route("/scheduler/disable", methods=["POST"])
def scheduler_disable():
    """Disable the scheduler."""
    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({"error": "Scheduler not available"}), 500

    if scheduler.state == STATE_RUNNING:
        scheduler.pause()
        return jsonify({"status": "ok", "message": "Scheduler disabled"})
    else:
        return jsonify({"status": "ok", "message": f"Scheduler already paused (state: {scheduler.state})"})


@bp.route("/tasks", methods=["GET"])
def list_tasks():
    """List all timer tasks."""
    tasks = Task.query.order_by(Task.id.desc()).all()

    result = []
    for task in tasks:
        # Get APScheduler job info
        scheduler = get_scheduler()
        job = scheduler.get_job(str(task.id)) if scheduler else None

        task_info = {
            "id": task.id,
            "name": task.name,
            "project": task.project,
            "spider": task.spider,
            "cron_schedule": {
                "minute": task.minute,
                "hour": task.hour,
                "day": task.day,
                "month": task.month,
                "day_of_week": task.day_of_week,
            },
            "timezone": task.timezone,
            "create_time": task.create_time.isoformat() if task.create_time else None,
            "update_time": task.update_time.isoformat() if task.update_time else None,
            "status": "running" if job and job.next_run_time else ("paused" if job else "finished"),
            "next_run_time": str(job.next_run_time) if job and job.next_run_time else None,
        }
        result.append(task_info)

    return jsonify({"status": "ok", "count": len(result), "tasks": result})


@bp.route("/tasks", methods=["POST"])
def create_task():
    """Create a new timer task.

    Request body (JSON or form data):
        spider: Spider name (required)
        cron: Cron schedule "minute hour day month day_of_week" (required)
        project: Scrapyd project (default: "default")
        name: Task name (default: "{spider}_daily")
        builders: Comma-separated builder names (optional, for batdongsan spider)
        timezone: Schedule timezone (default: "UTC")
        action: "add", "add_fire", or "add_pause" (default: "add")
    """
    # Get data from JSON or form
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    # Required fields
    spider = data.get("spider")
    cron = data.get("cron") or data.get("schedule")

    if not spider:
        return jsonify({"error": "spider is required"}), 400
    if not cron:
        return jsonify({"error": "cron (or schedule) is required"}), 400

    # Parse cron string
    cron_parts = cron.split()
    if len(cron_parts) != 5:
        return jsonify({"error": f"Invalid cron format: {cron}. Expected 5 fields: minute hour day month day_of_week"}), 400

    # Optional fields
    project = data.get("project", "default")
    version = data.get("version", "default-the-latest-version")
    name = data.get("name") or f"{spider}_daily"
    timezone = data.get("timezone", "UTC")
    action = data.get("action", "add")  # add, add_fire, add_pause

    # Build settings/arguments
    settings_arguments = {"setting": []}
    builders = data.get("builders")
    if builders:
        settings_arguments["builders"] = builders

    try:
        # Create Task record
        task = Task()
        task.project = project
        task.version = version
        task.spider = spider
        task.jobid = ""
        task.settings_arguments = json.dumps(settings_arguments, sort_keys=True)
        task.selected_nodes = "[1]"

        task.name = name
        task.trigger = "cron"

        task.year = "*"
        task.month = cron_parts[3]
        task.day = cron_parts[2]
        task.week = "*"
        task.day_of_week = cron_parts[4]
        task.hour = cron_parts[1]
        task.minute = cron_parts[0]
        task.second = "0"

        task.start_date = None
        task.end_date = None
        task.timezone = timezone
        task.jitter = 0
        task.misfire_grace_time = 600
        task.coalesce = "True"
        task.max_instances = 1

        db.session.add(task)
        db.session.commit()
        task_id = task.id

        # Add to scheduler
        scheduler = get_scheduler()
        if not scheduler:
            return jsonify(
                {
                    "status": "warning",
                    "task_id": task_id,
                    "message": "Task created in database but scheduler not available. Restart ScrapydWeb.",
                }
            )

        from apscheduler.triggers.cron import CronTrigger
        from scrapydweb.views.operations.execute_task import execute_task

        trigger = CronTrigger(
            year="*",
            month=cron_parts[3],
            day=cron_parts[2],
            day_of_week=cron_parts[4],
            hour=cron_parts[1],
            minute=cron_parts[0],
            timezone=timezone,
        )

        # Determine next_run_time based on action
        next_run_time = None
        if action == "add_fire":
            next_run_time = datetime.now()
        elif action == "add_pause":
            next_run_time = None
        # else: add - use default (calculated from trigger)

        job_kwargs = {
            "func": execute_task,
            "kwargs": {"task_id": task_id},
            "id": str(task_id),
            "name": name,
            "trigger": trigger,
            "replace_existing": True,
            "misfire_grace_time": 600,
            "coalesce": True,
            "max_instances": 1,
        }

        if action == "add_pause":
            job_kwargs["next_run_time"] = None
        elif action == "add_fire":
            job_kwargs["next_run_time"] = datetime.now()

        job = scheduler.add_job(**job_kwargs)

        logger.info(f"Created timer task #{task_id}: {name} with schedule {cron}")

        return jsonify(
            {
                "status": "ok",
                "task_id": task_id,
                "name": name,
                "spider": spider,
                "schedule": cron,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
                "message": f"Timer task #{task_id} created successfully",
            }
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create timer task: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    """Get task details."""
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"error": f"Task #{task_id} not found"}), 404

    scheduler = get_scheduler()
    job = scheduler.get_job(str(task_id)) if scheduler else None

    return jsonify(
        {
            "status": "ok",
            "task": {
                "id": task.id,
                "name": task.name,
                "project": task.project,
                "version": task.version,
                "spider": task.spider,
                "cron_schedule": {
                    "minute": task.minute,
                    "hour": task.hour,
                    "day": task.day,
                    "month": task.month,
                    "day_of_week": task.day_of_week,
                },
                "timezone": task.timezone,
                "settings_arguments": json.loads(task.settings_arguments),
                "selected_nodes": json.loads(task.selected_nodes),
                "create_time": task.create_time.isoformat() if task.create_time else None,
                "update_time": task.update_time.isoformat() if task.update_time else None,
                "job_status": "running" if job and job.next_run_time else ("paused" if job else "finished"),
                "next_run_time": str(job.next_run_time) if job and job.next_run_time else None,
            },
        }
    )


@bp.route("/tasks/<int:task_id>/fire", methods=["POST"])
def fire_task(task_id):
    """Fire a task immediately."""
    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({"error": "Scheduler not available"}), 500

    job = scheduler.get_job(str(task_id))
    if not job:
        return jsonify({"error": f"Task #{task_id} not found in scheduler"}), 404

    job.modify(next_run_time=datetime.now())

    return jsonify({"status": "ok", "message": f"Task #{task_id} fired", "next_run_time": str(datetime.now())})


@bp.route("/tasks/<int:task_id>/pause", methods=["POST"])
def pause_task(task_id):
    """Pause a task."""
    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({"error": "Scheduler not available"}), 500

    job = scheduler.get_job(str(task_id))
    if not job:
        return jsonify({"error": f"Task #{task_id} not found in scheduler"}), 404

    job.pause()

    return jsonify({"status": "ok", "message": f"Task #{task_id} paused"})


@bp.route("/tasks/<int:task_id>/resume", methods=["POST"])
def resume_task(task_id):
    """Resume a paused task."""
    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({"error": "Scheduler not available"}), 500

    job = scheduler.get_job(str(task_id))
    if not job:
        return jsonify({"error": f"Task #{task_id} not found in scheduler"}), 404

    job.resume()

    return jsonify({"status": "ok", "message": f"Task #{task_id} resumed", "next_run_time": str(job.next_run_time)})


@bp.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    """Delete a task."""
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"error": f"Task #{task_id} not found"}), 404

    # Remove from scheduler
    scheduler = get_scheduler()
    if scheduler:
        job = scheduler.get_job(str(task_id))
        if job:
            job.remove()

    # Delete from database
    db.session.delete(task)
    db.session.commit()

    return jsonify({"status": "ok", "message": f"Task #{task_id} deleted"})
