"""
Web Dashboard - Real-time monitoring interface for the Immune System
"""
import asyncio
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
import threading
import time
from .logging_config import get_logger

logger = get_logger("web_dashboard")


class WebDashboard:
    """Web dashboard for real-time monitoring"""

    def __init__(self, orchestrator, port=8090):
        self.orchestrator = orchestrator
        self.port = port
        self._loop = None  # Set from main() for thread-safe heal scheduling
        self.app = Flask(__name__)
        CORS(self.app)

        # Register routes
        self.app.route('/')(self.index)
        self.app.route('/api/status')(self.get_status)
        self.app.route('/api/agents')(self.get_agents)
        self.app.route('/api/infections')(self.get_infections)
        self.app.route('/api/healings')(self.get_healings)
        self.app.route('/api/stats')(self.get_stats)
        self.app.route('/api/pending-approvals')(self.get_pending_approvals)
        self.app.route('/api/approve-healing', methods=['POST'])(self.post_approve_healing)
        self.app.route('/api/approve-all', methods=['POST'])(self.post_approve_all)
        self.app.route('/api/rejected-approvals')(self.get_rejected_approvals)
        self.app.route('/api/heal-explicitly', methods=['POST'])(self.post_heal_explicitly)
        self.app.route('/api/heal-all-rejected', methods=['POST'])(self.post_heal_all_rejected)
        self.app.route('/api/v1/ingest', methods=['POST'])(self.post_ingest)
        self.app.route('/api/v1/agents/register', methods=['POST'])(self.post_register_agent)

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the asyncio event loop so approve-healing can schedule heal from Flask thread."""
        self._loop = loop
    
    def index(self):
        """Serve the main dashboard HTML"""
        return render_template_string(HTML_TEMPLATE)
    
    def get_status(self):
        """Get overall system status (includes healing_in_progress for UI)."""
        return jsonify({
            'running': self.orchestrator.running,
            'baselines_learned': self.orchestrator.baselines_learned,
            'runtime': time.time() - self.orchestrator.start_time,
            'healing_in_progress': list(self.orchestrator.healing_in_progress),
        })
    
    def get_agents(self):
        """Get all agents with their current status"""
        rejected_ids = {r['agent_id'] for r in self.orchestrator.get_rejected_approvals()}
        pending_ids = {p['agent_id'] for p in self.orchestrator.get_pending_approvals()}
        agents_data = []
        for agent_id, agent in self.orchestrator.agents.items():
            baseline = self.orchestrator.baseline_learner.get_baseline(agent_id)
            latest = self.orchestrator.telemetry.get_latest(agent_id)

            agents_data.append({
                'id': agent_id,
                'type': agent.agent_type,
                'model': getattr(agent, 'model_name', 'GPT-4'),
                'mcp_servers': getattr(agent, 'mcp_servers', []),
                'status': agent.status.value,
                'infected': agent.infected,
                'infection_type': agent.infection_type,
                'executions': self.orchestrator.telemetry.get_count(agent_id),
                'has_baseline': baseline is not None,
                'healing_rejected': agent_id in rejected_ids,
                'healing_pending': agent_id in pending_ids,
                'latest_metrics': {
                    'latency': latest.latency_ms if latest else 0,
                    'tokens': latest.token_count if latest else 0,
                    'input_tokens': latest.input_tokens if latest else 0,
                    'output_tokens': latest.output_tokens if latest else 0,
                    'cost': latest.cost if latest else 0.0,
                    'tools': latest.tool_calls if latest else 0,
                    'model': latest.model if latest else '',
                    'error_type': latest.error_type if latest else '',
                } if latest else None
            })

        return jsonify(agents_data)
    
    def get_infections(self):
        """Get infection history"""
        return jsonify({
            'total': self.orchestrator.total_infections,
            'current_quarantined': list(self.orchestrator.quarantine.get_all_quarantined())
        })
    
    def get_healings(self):
        """Get recent healing actions (user + system) for Recent Healing Actions UI"""
        return jsonify(self.orchestrator.get_healing_actions())
    
    def get_pending_approvals(self):
        """Get severe infections awaiting UI approval."""
        return jsonify(self.orchestrator.get_pending_approvals())

    def get_rejected_approvals(self):
        """Get agents whose healing was rejected (waiting for 'Heal now')."""
        return jsonify(self.orchestrator.get_rejected_approvals())

    def post_heal_explicitly(self):
        """Start healing directly for an agent that had healing rejected (POST JSON: agent_id)."""
        data = request.get_json(silent=True) or {}
        agent_id = data.get('agent_id')
        if not agent_id:
            return jsonify({'ok': False, 'error': 'agent_id required'}), 400
        infection = self.orchestrator.start_healing_explicitly(agent_id)
        if infection and self._loop:
            asyncio.run_coroutine_threadsafe(
                self.orchestrator.heal_agent(agent_id, infection, trigger="explicit_after_reject"),
                self._loop,
            )
        return jsonify({'ok': infection is not None})

    def post_heal_all_rejected(self):
        """Start healing for all rejected agents (Heal all)."""
        healed_list = self.orchestrator.start_healing_all_rejected()
        if healed_list and self._loop:
            for agent_id, infection in healed_list:
                asyncio.run_coroutine_threadsafe(
                    self.orchestrator.heal_agent(agent_id, infection, trigger="explicit_after_reject"),
                    self._loop,
                )
        return jsonify({'ok': True, 'healed_count': len(healed_list)})

    def post_approve_healing(self):
        """Approve or reject healing for a severe infection (POST JSON: agent_id, approved)."""
        data = request.get_json(silent=True) or {}
        agent_id = data.get('agent_id')
        approved = data.get('approved', False)
        if not agent_id:
            return jsonify({'ok': False, 'error': 'agent_id required'}), 400
        infection, did_approve = self.orchestrator.approve_healing(agent_id, approved)
        if did_approve and infection and self._loop:
            asyncio.run_coroutine_threadsafe(
                self.orchestrator.heal_agent(agent_id, infection, trigger="after_approval"),
                self._loop,
            )
        return jsonify({'ok': True, 'approved': did_approve})

    def post_approve_all(self):
        """Approve or reject all pending approvals (POST JSON: approved)."""
        data = request.get_json(silent=True) or {}
        approved = data.get('approved', False)
        pending_count = len(self.orchestrator.get_pending_approvals())
        approved_list = self.orchestrator.approve_all_pending(approved)
        if approved and approved_list and self._loop:
            for agent_id, infection in approved_list:
                asyncio.run_coroutine_threadsafe(
                    self.orchestrator.heal_agent(agent_id, infection, trigger="after_approval"),
                    self._loop,
                )
        return jsonify({
            'ok': True,
            'approved_count': len(approved_list),
            'rejected_count': 0 if approved else pending_count,
        })

    # ---- External agent integration endpoints ----

    def post_ingest(self):
        """Ingest vitals from an external (real) AI agent.

        POST JSON body:
            agent_id (required), agent_type, latency_ms, input_tokens,
            output_tokens, token_count, tool_calls, retries, success,
            cost, model, error_type, prompt_hash.
        """
        data = request.get_json(silent=True) or {}
        agent_id = data.get('agent_id')
        if not agent_id:
            return jsonify({'ok': False, 'error': 'agent_id is required'}), 400

        # Auto-register unknown agents so external agents don't need a separate call
        if agent_id not in self.orchestrator.agents:
            from .agents import BaseAgent
            agent = BaseAgent(
                agent_id=agent_id,
                agent_type=data.get('agent_type', 'external'),
                model_name=data.get('model', 'unknown'),
            )
            self.orchestrator.agents[agent_id] = agent

        input_tokens = int(data.get('input_tokens', 0))
        output_tokens = int(data.get('output_tokens', 0))
        token_count = int(data.get('token_count', 0)) or (input_tokens + output_tokens)

        vitals_dict = {
            'agent_id': agent_id,
            'agent_type': data.get('agent_type', 'external'),
            'latency_ms': int(data.get('latency_ms', 0)),
            'token_count': token_count,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cost': float(data.get('cost', 0.0)),
            'tool_calls': int(data.get('tool_calls', 0)),
            'retries': int(data.get('retries', 0)),
            'success': bool(data.get('success', True)),
            'model': data.get('model', ''),
            'error_type': data.get('error_type', ''),
            'prompt_hash': data.get('prompt_hash', ''),
            'timestamp': data.get('timestamp', time.time()),
        }
        self.orchestrator.telemetry.record(vitals_dict)
        return jsonify({'ok': True})

    def post_register_agent(self):
        """Register an external agent with the immune system.

        POST JSON body: agent_id (required), agent_type, model.
        """
        data = request.get_json(silent=True) or {}
        agent_id = data.get('agent_id')
        if not agent_id:
            return jsonify({'ok': False, 'error': 'agent_id is required'}), 400

        if agent_id in self.orchestrator.agents:
            return jsonify({'ok': True, 'status': 'already_registered'})

        from .agents import BaseAgent
        agent = BaseAgent(
            agent_id=agent_id,
            agent_type=data.get('agent_type', 'external'),
            model_name=data.get('model', 'unknown'),
        )
        self.orchestrator.agents[agent_id] = agent
        return jsonify({'ok': True, 'status': 'registered'})

    def get_stats(self):
        """Get overall statistics"""
        patterns = self.orchestrator.immune_memory.get_pattern_summary()
        runtime_seconds = time.time() - self.orchestrator.start_time
        current_infected = sum(1 for agent in self.orchestrator.agents.values() if agent.infected)
        
        return jsonify({
            'total_agents': len(self.orchestrator.agents),
            'total_executions': self.orchestrator.telemetry.total_executions,
            'runtime': runtime_seconds,
            'baselines_learned': self.orchestrator.baseline_learner.count_baselines(),
            'total_infections': self.orchestrator.total_infections,
            'current_infected': current_infected,
            'total_healed': self.orchestrator.total_healed,
            'failed_healings': self.orchestrator.total_failed_healings,
            'total_quarantines': self.orchestrator.quarantine.total_quarantines,
            'current_quarantined': self.orchestrator.quarantine.get_quarantined_count(),
            # Success rate = share of detected infections that were successfully healed
            'success_rate': (self.orchestrator.total_healed / self.orchestrator.total_infections) if self.orchestrator.total_infections else self.orchestrator.immune_memory.get_success_rate(),
            'immune_records': self.orchestrator.immune_memory.get_total_healings(),
            'learned_patterns': patterns
        })
    
    def start(self):
        """Start the web server in a background thread"""
        def run():
            logger.info("Web Dashboard: http://localhost:%d", self.port)
            self.app.run(host='0.0.0.0', port=self.port, debug=False, use_reloader=False)
        
        thread = threading.Thread(target=run, daemon=True)
        thread.start()


# HTML Template for the dashboard
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>🛡️ AI Agent Immune System Dashboard</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            color: #333;
            padding: 20px;
            min-height: 100vh;
            position: relative;
            overflow-x: hidden;
        }
        
        /* Thematic background: immune system / defense / monitoring */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            z-index: -2;
            background: linear-gradient(135deg, #0f2027 0%, #203a43 35%, #2c5364 70%, #1a1a2e 100%);
            background-size: 400% 400%;
            animation: gradientShift 20s ease infinite;
        }
        
        @keyframes gradientShift {
            0%, 100% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
        }
        
        /* Subtle hexagonal "cell" pattern overlay */
        body::after {
            content: '';
            position: fixed;
            inset: 0;
            z-index: -1;
            opacity: 0.08;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='98' viewBox='0 0 56 98'%3E%3Cpath fill='none' stroke='%23ffffff' stroke-width='0.5' d='M28 0L56 14v28L28 70L0 42V14L28 0z'/%3E%3C/svg%3E");
            background-size: 56px 98px;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        
        .header {
            background: rgba(255, 255, 255, 0.97);
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.15);
            border-left: 4px solid #2dd4bf;
        }
        
        .header h1 {
            font-size: 2em;
            margin-bottom: 10px;
            color: #0f2027;
        }
        
        .header .subtitle {
            color: #475569;
            font-size: 1.1em;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .stat-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.12);
            transition: transform 0.2s;
        }
        
        .stat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.18);
        }
        
        .stat-card .label {
            color: #64748b;
            font-size: 0.9em;
            margin-bottom: 8px;
        }
        
        .stat-card .value {
            font-size: 2em;
            font-weight: bold;
            color: #0f766e;
        }
        
        .agents-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .agent-card {
            background: rgba(255, 255, 255, 0.98);
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        
        .agent-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.15);
        }
        
        .agent-card.healthy {
            border-left: 4px solid #4CAF50;
        }
        
        .agent-card.infected {
            border-left: 4px solid #f44336;
            animation: pulse 2s infinite;
        }
        
        .agent-card.quarantined {
            border-left: 4px solid #FF9800;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; }
        }
        
        .agent-card .agent-id {
            font-weight: bold;
            font-size: 1.1em;
            margin-bottom: 4px;
        }
        
        .agent-card .agent-model {
            font-size: 0.85em;
            color: #0f766e;
            font-weight: 600;
            margin-bottom: 2px;
        }
        
        .agent-card .agent-type {
            color: #64748b;
            font-size: 0.9em;
            margin-bottom: 6px;
        }
        
        .agent-card .agent-mcp {
            font-size: 0.75em;
            color: #64748b;
            margin-bottom: 8px;
            font-family: ui-monospace, monospace;
        }
        
        .agent-card .status {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.85em;
            font-weight: 500;
            margin-bottom: 10px;
        }
        
        .status.healthy {
            background: #E8F5E9;
            color: #2E7D32;
        }
        
        .status.infected {
            background: #FFEBEE;
            color: #C62828;
        }
        
        .status.quarantined {
            background: #FFF3E0;
            color: #E65100;
        }
        
        .agent-card .metrics {
            font-size: 0.85em;
            color: #666;
            margin-top: 10px;
            line-height: 1.5;
        }
        
        .section {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 20px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.12);
        }
        
        .section h2 {
            margin-bottom: 20px;
            color: #0f2027;
        }
        
        .section.collapsible .section-header-toggle {
            display: flex;
            align-items: center;
            justify-content: space-between;
            cursor: pointer;
            user-select: none;
            margin-bottom: 0;
        }
        
        .section.collapsible .section-header-toggle h2 {
            margin-bottom: 0;
        }
        
        .section.collapsible .section-arrow {
            font-size: 0.9em;
            color: #64748b;
            transition: transform 0.2s;
        }
        
        .section.collapsible.collapsed .section-arrow {
            transform: rotate(-90deg);
        }
        
        .section.collapsible .section-header-toggle:hover {
            opacity: 0.9;
        }
        
        .section.collapsible .section-body {
            overflow: hidden;
            margin-top: 16px;
        }
        
        .section.collapsible.collapsed .section-body {
            display: none;
        }
        
        .healing-record {
            padding: 12px;
            margin-bottom: 10px;
            border-radius: 6px;
            background: #f5f5f5;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .healing-record.success {
            border-left: 4px solid #4CAF50;
        }
        
        .healing-record.failed {
            border-left: 4px solid #f44336;
        }
        
        .healing-record.user-action {
            border-left: 4px solid #2196F3;
            background: #E3F2FD;
        }
        
        .healing-record.user-rejected {
            border-left: 4px solid #f44336;
            background: #FFEBEE;
        }
        
        .healing-record.retry {
            border-left: 4px solid #FF9800;
            background: #FFF3E0;
        }
        
        .healing-record.approval-requested {
            border-left: 4px solid #FFC107;
            background: #FFFDE7;
        }
        
        .healing-record .trigger-badge {
            font-size: 0.75em;
            padding: 2px 8px;
            border-radius: 10px;
            margin-left: 6px;
            background: #E8EAF6;
            color: #3F51B5;
        }
        
        .healing-record .trigger-badge.auto {
            background: #E8F5E9;
            color: #2E7D32;
        }
        
        .healing-record .trigger-badge.after-approval {
            background: #E3F2FD;
            color: #1976D2;
        }
        
        .healing-record .trigger-badge.explicit-reject {
            background: #FFF3E0;
            color: #E65100;
        }
        
        .badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: 500;
        }
        
        .badge.success {
            background: #E8F5E9;
            color: #2E7D32;
        }
        
        .badge.failed {
            background: #FFEBEE;
            color: #C62828;
        }
        
        .learned-patterns {
            display: grid;
            gap: 10px;
        }
        
        .pattern-item {
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
            border-left: 4px solid #2dd4bf;
        }
        
        .pattern-item strong {
            color: #0f766e;
        }
        
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #999;
        }
        
        .healing-progress-banner {
            background: linear-gradient(90deg, #FFF3E0 0%, #FFE0B2 100%);
            border: 1px solid #FF9800;
            border-radius: 10px;
            padding: 14px 20px;
            margin-bottom: 20px;
            font-weight: 500;
            color: #E65100;
            box-shadow: 0 2px 4px rgba(0,0,0,0.08);
        }
        
        .section-desc {
            color: #666;
            font-size: 0.95em;
            margin-bottom: 15px;
        }
        
        .pending-approval-card {
            background: #FFF8E1;
            border: 1px solid #FFC107;
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }
        
        .pending-approval-card .info {
            flex: 1;
            min-width: 160px;
        }
        
        .pending-approval-card .agent-id {
            font-weight: bold;
            color: #F57C00;
            margin-bottom: 2px;
        }
        
        .pending-approval-card .meta {
            font-size: 0.85em;
            color: #666;
            margin-top: 4px;
        }
        
        .pending-actions-row {
            display: flex;
            gap: 10px;
            margin-bottom: 12px;
        }
        
        .btn-approve-all, .btn-reject-all {
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 0.9em;
            font-weight: 600;
            cursor: pointer;
            border: none;
        }
        
        .btn-approve-all {
            background: #4CAF50;
            color: white;
        }
        
        .btn-approve-all:hover {
            background: #43A047;
        }
        
        .btn-reject-all {
            background: #f44336;
            color: white;
        }
        
        .btn-reject-all:hover {
            background: #E53935;
        }
        
        .rejected-actions-row {
            display: flex;
            gap: 10px;
            margin-bottom: 12px;
        }
        
        .btn-heal-all {
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 0.9em;
            font-weight: 600;
            cursor: pointer;
            border: none;
            background: #2196F3;
            color: white;
        }
        
        .btn-heal-all:hover {
            background: #1976D2;
        }
        
        .pending-approval-actions {
            display: flex;
            gap: 8px;
        }
        
        .btn-approve, .btn-reject {
            width: 32px;
            height: 32px;
            padding: 0;
            border-radius: 6px;
            font-size: 1.1em;
            line-height: 1;
            cursor: pointer;
            border: none;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }
        
        .btn-approve::after, .btn-reject::after {
            content: attr(data-tooltip);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%) translateY(-6px);
            padding: 4px 8px;
            font-size: 0.75em;
            font-weight: 600;
            white-space: nowrap;
            background: #333;
            color: #fff;
            border-radius: 4px;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.15s ease, transform 0.15s ease;
        }
        
        .btn-approve:hover::after, .btn-reject:hover::after {
            opacity: 1;
            transform: translateX(-50%) translateY(-4px);
        }
        
        .btn-approve {
            background: #4CAF50;
            color: white;
        }
        
        .btn-approve:hover {
            background: #43A047;
        }
        
        .btn-reject {
            background: #f44336;
            color: white;
        }
        
        .btn-reject:hover {
            background: #E53935;
        }
        
        .btn-retry {
            padding: 8px 18px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            border: none;
            font-size: 0.9em;
            background: #2196F3;
            color: white;
        }
        
        .btn-retry:hover {
            background: #1976D2;
        }
        
        .btn-retry-inline {
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.8em;
            font-weight: 600;
            cursor: pointer;
            border: none;
            background: #2196F3;
            color: white;
            margin-top: 8px;
        }
        
        .btn-retry-inline:hover {
            background: #1976D2;
        }
        
        .rejected-badge {
            font-size: 0.85em;
            color: #E65100;
            margin-top: 6px;
            font-weight: 500;
        }
        
        .agent-card-rejected-block {
            margin-top: 10px;
            padding: 10px 12px;
            background: #FFF3E0;
            border: 1px solid #FF9800;
            border-radius: 8px;
            width: 100%;
        }
        
        .agent-card-rejected-block .rejected-label {
            font-size: 0.85em;
            color: #E65100;
            font-weight: 600;
            margin-bottom: 8px;
            display: block;
        }
        
        .agent-card-rejected-block .btn-retry-inline {
            width: 100%;
            margin-top: 0;
            padding: 8px 12px;
        }
        
        .agent-card.rejected-state {
            border-left: 4px solid #E65100;
            background: #FFF8E1;
        }
        
        .agent-card-pending-block {
            margin-top: 8px;
            padding: 6px 10px;
            background: #FFF8E1;
            border: 1px solid #FFC107;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }
        
        .agent-card-pending-block .pending-label {
            font-size: 0.8em;
            color: #F57C00;
            font-weight: 600;
        }
        
        .agent-card-pending-block .pending-actions {
            display: flex;
            gap: 6px;
        }
        
        .agent-card-pending-block .btn-approve-inline,
        .agent-card-pending-block .btn-reject-inline {
            width: 28px;
            height: 28px;
            padding: 0;
            border-radius: 6px;
            font-size: 1em;
            line-height: 1;
            cursor: pointer;
            border: none;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }
        
        .agent-card-pending-block .btn-approve-inline::after,
        .agent-card-pending-block .btn-reject-inline::after {
            content: attr(data-tooltip);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%) translateY(-6px);
            padding: 4px 8px;
            font-size: 0.75em;
            font-weight: 600;
            white-space: nowrap;
            background: #333;
            color: #fff;
            border-radius: 4px;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.15s ease, transform 0.15s ease;
        }
        
        .agent-card-pending-block .btn-approve-inline:hover::after,
        .agent-card-pending-block .btn-reject-inline:hover::after {
            opacity: 1;
            transform: translateX(-50%) translateY(-4px);
        }
        
        .agent-card-pending-block .btn-approve-inline {
            background: #4CAF50;
            color: white;
        }
        
        .agent-card-pending-block .btn-approve-inline:hover {
            background: #43A047;
        }
        
        .agent-card-pending-block .btn-reject-inline {
            background: #f44336;
            color: white;
        }
        
        .agent-card-pending-block .btn-reject-inline:hover {
            background: #E53935;
        }
        
        .agent-card.pending-state {
            border-left: 4px solid #FFC107;
            background: #FFFDE7;
        }
        
        .rejected-card {
            background: #FFEBEE;
            border-color: #f44336;
        }
        
        .agent-card.healing {
            border-left: 4px solid #FF9800;
            animation: pulse 1.5s infinite;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🛡️ AI Agent Immune System</h1>
            <p class="subtitle">Monitoring and Healing System for <strong>AI agents</strong> (e.g. powered by GPT-5, Claude Sonnet, Gemini) using tools via <strong>MCP servers</strong> (filesystem, GitHub, Slack, Postgres, etc.). The immune system detects, quarantines, and heals them.</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="label">Total AI Agents</div>
                <div class="value" id="stat-agents">-</div>
            </div>
            <div class="stat-card">
                <div class="label">Executions</div>
                <div class="value" id="stat-executions">-</div>
            </div>
            <div class="stat-card">
                <div class="label">Infections</div>
                <div class="value" id="stat-infections">-</div>
            </div>
            <div class="stat-card">
                <div class="label">Healed</div>
                <div class="value" id="stat-healed">-</div>
            </div>
            <div class="stat-card" title="Total quarantine events (1 per infection). Less than Healed when some are still pending or run ended before healing.">
                <div class="label">Quarantined</div>
                <div class="value" id="stat-quarantined">-</div>
            </div>
            <div class="stat-card">
                <div class="label">Success Rate</div>
                <div class="value" id="stat-success">-</div>
            </div>
        </div>
        
        <div id="healing-progress-banner" class="healing-progress-banner" style="display: none;">
            ⏳ Healing in progress: <span id="healing-progress-agents"></span>
        </div>
        
        <div class="section collapsible collapsed" id="pending-approvals-section">
            <div class="section-header-toggle">
                <h2>⏸️ Pending Approvals (severe infections)</h2>
                <span class="section-arrow">▼</span>
            </div>
            <div class="section-body">
                <p class="section-desc">Severe infections require your approval before healing. Approve to start healing; reject to keep agent quarantined until you click "Heal now".</p>
                <div class="pending-actions-row" id="pending-actions-row" style="display: none;">
                    <button type="button" class="btn-approve-all" id="btn-approve-all" title="Approve healing for all pending">Approve all</button>
                    <button type="button" class="btn-reject-all" id="btn-reject-all" title="Reject healing for all pending">Reject all</button>
                </div>
                <div id="pending-approvals-list"></div>
            </div>
        </div>
        
        <div class="section collapsible collapsed" id="rejected-approvals-section">
            <div class="section-header-toggle">
                <h2>🚫 Rejected Healings</h2>
                <span class="section-arrow">▼</span>
            </div>
            <div class="section-body">
                <p class="section-desc">Healing was rejected for these agents. They stay quarantined. Click "Heal now" to start healing.</p>
                <div class="rejected-actions-row" id="rejected-actions-row" style="display: none;">
                    <button type="button" class="btn-heal-all" id="btn-heal-all" title="Start healing for all rejected">Heal all</button>
                </div>
                <div id="rejected-approvals-list"></div>
            </div>
        </div>
        
        <div class="section collapsible" id="agents-grid-section">
            <div class="section-header-toggle">
                <h2>🤖 AI Agent Status Grid</h2>
                <span class="section-arrow">▼</span>
            </div>
            <div class="section-body">
                <p class="section-desc">Each card is an <strong>AI agent</strong> with a model (GPT-5, Claude Sonnet, etc.) and MCP servers (tools). Status shows whether it is healthy, infected, or quarantined.</p>
                <div class="agents-grid" id="agents-grid"></div>
            </div>
        </div>
        
        <div class="section collapsible" id="healings-section">
            <div class="section-header-toggle">
                <h2>💊 Recent Healing Actions</h2>
                <span class="section-arrow">▼</span>
            </div>
            <div class="section-body">
                <p class="section-desc">User decisions (approved/rejected/retry) and healing attempts (auto-healed or after approval).</p>
                <div id="healings-list"></div>
            </div>
        </div>
        
        <div class="section collapsible" id="patterns-section">
            <div class="section-header-toggle">
                <h2>🧠 Learned Healing Patterns</h2>
                <span class="section-arrow">▼</span>
            </div>
            <div class="section-body">
                <div class="learned-patterns" id="patterns-list"></div>
            </div>
        </div>
    </div>
    
    <script>
        async function fetchData() {
            try {
                const [stats, agents, healings, status, pendingApprovals] = await Promise.all([
                    fetch('/api/stats').then(r => r.json()),
                    fetch('/api/agents').then(r => r.json()),
                    fetch('/api/healings').then(r => r.json()),
                    fetch('/api/status').then(r => r.json()),
                    fetch('/api/pending-approvals').then(r => r.json())
                ]);
                
                updateStats(stats);
                updateAgents(agents, status.healing_in_progress || []);
                updateHealings(healings);
                updatePatterns(stats.learned_patterns);
                updateHealingProgress(status.healing_in_progress || []);
                updatePendingApprovals(pendingApprovals);
                const rejectedApprovals = await fetch('/api/rejected-approvals').then(r => r.json());
                updateRejectedApprovals(rejectedApprovals);
            } catch (e) {
                console.error('Error fetching data:', e);
            }
        }
        
        function updateHealingProgress(agentIds) {
            const banner = document.getElementById('healing-progress-banner');
            const el = document.getElementById('healing-progress-agents');
            if (agentIds.length === 0) {
                banner.style.display = 'none';
                return;
            }
            el.textContent = agentIds.join(', ');
            banner.style.display = 'block';
        }
        
        function updatePendingApprovals(list) {
            const container = document.getElementById('pending-approvals-list');
            const actionsRow = document.getElementById('pending-actions-row');
            const section = document.getElementById('pending-approvals-section');
            if (!list || list.length === 0) {
                if (actionsRow) actionsRow.style.display = 'none';
                container.innerHTML = '<div class="empty-state">No pending approvals</div>';
                if (section) section.classList.add('collapsed');
                return;
            }
            if (section) section.classList.remove('collapsed');
            if (actionsRow) actionsRow.style.display = 'flex';
            container.innerHTML = list.map(p => `
                <div class="pending-approval-card" data-agent-id="${p.agent_id}">
                    <div class="info">
                        <div class="agent-id">${p.agent_id}</div>
                        <div>Severity: ${p.severity}/10 · ${p.diagnosis_type}</div>
                        <div class="meta">${p.anomalies.join(', ')}</div>
                        <div class="meta" style="margin-top: 4px;">${p.reasoning}</div>
                    </div>
                    <div class="pending-approval-actions">
                        <button class="btn-approve" data-tooltip="Heal" onclick="approveHealing('${p.agent_id}', true)" title="Heal">✓</button>
                        <button class="btn-reject" data-tooltip="Reject" onclick="approveHealing('${p.agent_id}', false)" title="Reject healing">✗</button>
                    </div>
                </div>
            `).join('');
        }
        
        async function approveHealing(agentId, approved) {
            try {
                const res = await fetch('/api/approve-healing', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ agent_id: agentId, approved: approved })
                });
                const data = await res.json();
                if (data.ok) {
                    fetchData();
                }
            } catch (e) {
                console.error('Approve/reject failed:', e);
            }
        }
        
        function updateRejectedApprovals(list) {
            const container = document.getElementById('rejected-approvals-list');
            const actionsRow = document.getElementById('rejected-actions-row');
            const section = document.getElementById('rejected-approvals-section');
            if (!list || list.length === 0) {
                if (actionsRow) actionsRow.style.display = 'none';
                container.innerHTML = '<div class="empty-state">No rejected healings</div>';
                if (section) section.classList.add('collapsed');
                return;
            }
            if (section) section.classList.remove('collapsed');
            if (actionsRow) actionsRow.style.display = 'flex';
            container.innerHTML = list.map(p => `
                <div class="pending-approval-card rejected-card">
                    <div class="info">
                        <div class="agent-id">${p.agent_id}</div>
                        <div>Severity: ${p.severity}/10 · ${p.diagnosis_type}</div>
                        <div class="meta">${p.anomalies.join(', ')}</div>
                    </div>
                    <button class="btn-retry" onclick="healExplicitly('${p.agent_id}')">Heal now</button>
                </div>
            `).join('');
        }
        
        async function healExplicitly(agentId) {
            try {
                const res = await fetch('/api/heal-explicitly', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ agent_id: agentId })
                });
                const data = await res.json();
                if (data.ok) {
                    fetchData();
                }
            } catch (e) {
                console.error('Heal now failed:', e);
            }
        }
        
        function updateStats(stats) {
            document.getElementById('stat-agents').textContent = stats.total_agents;
            document.getElementById('stat-executions').textContent = stats.total_executions;
            document.getElementById('stat-infections').textContent = (stats.current_infected ?? stats.total_infections);
            document.getElementById('stat-healed').textContent = stats.total_healed;
            document.getElementById('stat-quarantined').textContent = stats.total_quarantines;
            document.getElementById('stat-success').textContent = (stats.success_rate * 100).toFixed(0) + '%';
        }
        
        function updateAgents(agents, healingInProgress) {
            const healingSet = new Set(healingInProgress || []);
            const grid = document.getElementById('agents-grid');
            grid.innerHTML = agents.map(agent => {
                const isHealing = healingSet.has(agent.id);
                const isRejected = !!agent.healing_rejected;
                const isPending = !!agent.healing_pending;
                const cardClass = isPending ? `${agent.status} pending-state` : (isRejected ? `${agent.status} rejected-state` : (isHealing ? `${agent.status} healing` : agent.status));
                const pendingHtml = isPending
                    ? `<div class="agent-card-pending-block">
                        <span class="pending-label">Action required</span>
                        <div class="pending-actions">
                            <button type="button" class="btn-approve-inline" data-approve="true" data-tooltip="Heal" title="Heal">✓</button>
                            <button type="button" class="btn-reject-inline" data-approve="false" data-tooltip="Reject" title="Reject healing">✗</button>
                        </div>
                       </div>`
                    : '';
                const rejectedHtml = isRejected
                    ? `<div class="agent-card-rejected-block">
                        <span class="rejected-label">Healing was rejected</span>
                        <button type="button" class="btn-retry-inline">Heal now</button>
                       </div>`
                    : '';
                const mcpStr = (agent.mcp_servers && agent.mcp_servers.length) ? agent.mcp_servers.slice(0, 3).join(', ') : '';
                return `
                <div class="agent-card ${cardClass}" data-agent-id="${(agent.id || '').replace(/"/g, '&quot;')}">
                    <div class="agent-id">${agent.id}</div>
                    <div class="agent-model">${agent.model || 'GPT-4'}</div>
                    <div class="agent-type">${agent.type} agent</div>
                    ${mcpStr ? `<div class="agent-mcp">MCP: ${mcpStr}</div>` : ''}
                    <span class="status ${agent.status}">${agent.status.toUpperCase()}</span>
                    ${isHealing ? '<span class="status" style="background:#FFF3E0;color:#E65100;margin-left:6px;">HEALING</span>' : ''}
                    ${agent.infected ? `<div style="color: #f44336; font-size: 0.85em; margin-top: 5px;">⚠️ ${agent.infection_type || 'infected'}</div>` : ''}
                    ${pendingHtml}
                    ${rejectedHtml}
                    <div class="metrics">
                        📊 ${agent.executions} executions<br>
                        ${agent.has_baseline ? '✅ Baseline learned' : '⏳ Learning...'}
                        ${agent.latest_metrics ? `<br>⏱ ${agent.latest_metrics.latency}ms | 🔤 ${agent.latest_metrics.tokens} tokens | 🔧 ${agent.latest_metrics.tools} tools` : ''}
                    </div>
                </div>
            `}).join('');
        }
        
        function formatHealingAction(h) {
            const agent = h.agent_id;
            const ts = h.timestamp ? new Date(h.timestamp * 1000).toLocaleTimeString() : '';
            switch (h.type) {
                case 'approval_requested':
                    return { rowClass: 'approval-requested', html: `
                        <div><strong>${agent}</strong>: Approval requested (severity ${h.severity}/10)</div>
                        <div style="font-size: 0.8em; color: #666;">⏸️ Awaiting user decision</div>
                    `, badge: '⏸️ Pending' };
                case 'user_approved':
                    return { rowClass: 'user-action', html: `
                        <div><strong>${agent}</strong>: User approved healing</div>
                        <div style="font-size: 0.8em; color: #666;">Healing started after approval</div>
                    `, badge: '✅ Approved' };
                case 'user_rejected':
                    return { rowClass: 'user-rejected', html: `
                        <div><strong>${agent}</strong>: User rejected healing</div>
                        <div style="font-size: 0.8em; color: #666;">Agent stays quarantined until you choose Heal now</div>
                    `, badge: '🚫 Rejected' };
                case 'explicit_heal_requested':
                    return { rowClass: 'retry', html: `
                        <div><strong>${agent}</strong>: User chose Heal now</div>
                        <div style="font-size: 0.8em; color: #666;">Healing in progress</div>
                    `, badge: '💊 Heal' };
                case 'healing_attempt':
                    const triggerLabels = { after_approval: 'After approval', explicit_after_reject: 'Heal now', auto: 'Auto-healed' };
                    const triggerLabel = triggerLabels[h.trigger] || h.trigger || 'Auto-healed';
                    const triggerClasses = { after_approval: 'after-approval', explicit_after_reject: 'explicit-reject', auto: 'auto' };
                    const triggerClass = triggerClasses[h.trigger] || 'auto';
                    return { rowClass: h.success ? 'success' : 'failed', html: `
                        <div>
                            <strong>${agent}</strong>: ${h.action}
                            <span class="trigger-badge ${triggerClass}">${triggerLabel}</span>
                        </div>
                        <div style="font-size: 0.85em; color: #666; margin-top: 4px;">Diagnosis: ${h.diagnosis_type || ''}</div>
                    `, badge: h.success ? '✅ Success' : '❌ Failed' };
                default:
                    return { rowClass: '', html: `<div><strong>${agent}</strong>: ${h.type}</div>`, badge: '' };
            }
        }
        
        function updateHealings(healings) {
            const list = document.getElementById('healings-list');
            if (!healings || healings.length === 0) {
                list.innerHTML = '<div class="empty-state">No healing actions yet...</div>';
                return;
            }
            const reversed = [...healings].reverse();
            list.innerHTML = reversed.map(h => {
                const { rowClass, html, badge } = formatHealingAction(h);
                let badgeClass = '';
                if (h.type === 'healing_attempt') badgeClass = h.success ? 'success' : 'failed';
                else if (h.type === 'user_approved') badgeClass = 'success';
                else if (h.type === 'user_rejected') badgeClass = 'failed';
                return `<div class="healing-record ${rowClass}">${html}${badge ? `<span class="badge ${badgeClass}">${badge}</span>` : ''}</div>`;
            }).join('');
        }
        
        function updatePatterns(patterns) {
            const list = document.getElementById('patterns-list');
            const entries = Object.entries(patterns);
            
            if (entries.length === 0) {
                list.innerHTML = '<div class="empty-state">Learning patterns...</div>';
                return;
            }
            
            list.innerHTML = entries.map(([diagnosis, info]) => `
                <div class="pattern-item">
                    <strong>${diagnosis}</strong> → Best healing: <code>${info.best_action}</code> 
                    (${info.success_count} ${info.success_count === 1 ? 'success' : 'successes'})
                </div>
            `).join('');
        }
        
        // Auto-refresh every 1s (must match orchestrator.TICK_INTERVAL_SECONDS)
        setInterval(fetchData, 1000);
        
        document.getElementById('btn-approve-all').addEventListener('click', function() { approveAll(true); });
        document.getElementById('btn-reject-all').addEventListener('click', function() { approveAll(false); });
        document.getElementById('btn-heal-all').addEventListener('click', function() { healAllRejected(); });
        
        async function healAllRejected() {
            try {
                const res = await fetch('/api/heal-all-rejected', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
                const data = await res.json();
                if (data.ok) fetchData();
            } catch (e) {
                console.error('Heal all failed:', e);
            }
        }
        
        async function approveAll(approved) {
            try {
                const res = await fetch('/api/approve-all', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ approved: approved })
                });
                const data = await res.json();
                if (data.ok) fetchData();
            } catch (e) {
                console.error('Approve all / Reject all failed:', e);
            }
        }
        
        // Collapsible section headers
        document.querySelectorAll('.section-header-toggle').forEach(function(header) {
            header.addEventListener('click', function() {
                var section = this.closest('.section.collapsible');
                if (section) section.classList.toggle('collapsed');
            });
        });
        
        // Delegated click for Heal now
        document.getElementById('agents-grid').addEventListener('click', function(e) {
            const healBtn = e.target.closest('.btn-retry-inline');
            if (healBtn) {
                const card = e.target.closest('.agent-card');
                const agentId = card && card.dataset.agentId;
                if (agentId) healExplicitly(agentId);
                return;
            }
            const approveBtn = e.target.closest('.btn-approve-inline');
            const rejectBtn = e.target.closest('.btn-reject-inline');
            const approvalBtn = approveBtn || rejectBtn;
            if (approvalBtn) {
                const card = e.target.closest('.agent-card');
                const agentId = card && card.dataset.agentId;
                const approved = approvalBtn.getAttribute('data-approve') === 'true';
                if (agentId) approveHealing(agentId, approved);
            }
        });
        
        // Initial load
        fetchData();
    </script>
</body>
</html>
"""
