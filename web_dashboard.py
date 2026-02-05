"""
Web Dashboard - Real-time monitoring interface for the Immune System
"""
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import threading
import time


class WebDashboard:
    """Web dashboard for real-time monitoring"""
    
    def __init__(self, orchestrator, port=8090):
        self.orchestrator = orchestrator
        self.port = port
        self.app = Flask(__name__)
        CORS(self.app)
        
        # Register routes
        self.app.route('/')(self.index)
        self.app.route('/api/status')(self.get_status)
        self.app.route('/api/agents')(self.get_agents)
        self.app.route('/api/infections')(self.get_infections)
        self.app.route('/api/healings')(self.get_healings)
        self.app.route('/api/stats')(self.get_stats)
    
    def index(self):
        """Serve the main dashboard HTML"""
        return render_template_string(HTML_TEMPLATE)
    
    def get_status(self):
        """Get overall system status"""
        return jsonify({
            'running': self.orchestrator.running,
            'baselines_learned': self.orchestrator.baselines_learned,
            'runtime': time.time() - self.orchestrator.start_time
        })
    
    def get_agents(self):
        """Get all agents with their current status"""
        agents_data = []
        for agent_id, agent in self.orchestrator.agents.items():
            baseline = self.orchestrator.baseline_learner.get_baseline(agent_id)
            latest = self.orchestrator.telemetry.get_latest(agent_id)
            
            agents_data.append({
                'id': agent_id,
                'type': agent.agent_type,
                'status': agent.status.value,
                'infected': agent.infected,
                'infection_type': agent.infection_type,
                'executions': self.orchestrator.telemetry.get_count(agent_id),
                'has_baseline': baseline is not None,
                'latest_metrics': {
                    'latency': latest.latency_ms if latest else 0,
                    'tokens': latest.token_count if latest else 0,
                    'tools': latest.tool_calls if latest else 0
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
        """Get healing records from immune memory"""
        records = []
        for record in self.orchestrator.immune_memory.records[-20:]:  # Last 20
            records.append({
                'agent_id': record.agent_id,
                'diagnosis': record.diagnosis_type.value,
                'action': record.healing_action.value,
                'success': record.success,
                'timestamp': record.timestamp
            })
        
        return jsonify(records)
    
    def get_stats(self):
        """Get overall statistics"""
        patterns = self.orchestrator.immune_memory.get_pattern_summary()
        
        return jsonify({
            'total_agents': len(self.orchestrator.agents),
            'total_executions': self.orchestrator.telemetry.total_executions,
            'baselines_learned': len(self.orchestrator.baseline_learner.baselines),
            'total_infections': self.orchestrator.total_infections,
            'total_healed': self.orchestrator.total_healed,
            'failed_healings': self.orchestrator.total_failed_healings,
            'total_quarantines': self.orchestrator.quarantine.total_quarantines,
            'success_rate': self.orchestrator.immune_memory.get_success_rate(),
            'immune_records': self.orchestrator.immune_memory.get_total_healings(),
            'learned_patterns': patterns
        })
    
    def start(self):
        """Start the web server in a background thread"""
        def run():
            print(f"\n🌐 Web Dashboard: http://localhost:{self.port}", flush=True)
            print(f"   Open your browser and visit the URL above\n", flush=True)
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
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            padding: 20px;
            min-height: 100vh;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        
        .header {
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        .header h1 {
            font-size: 2em;
            margin-bottom: 10px;
            color: #667eea;
        }
        
        .header .subtitle {
            color: #666;
            font-size: 1.1em;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .stat-card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        
        .stat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        
        .stat-card .label {
            color: #666;
            font-size: 0.9em;
            margin-bottom: 8px;
        }
        
        .stat-card .value {
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
        }
        
        .agents-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .agent-card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        
        .agent-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
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
            margin-bottom: 5px;
        }
        
        .agent-card .agent-type {
            color: #666;
            font-size: 0.9em;
            margin-bottom: 10px;
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
            background: white;
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .section h2 {
            margin-bottom: 20px;
            color: #667eea;
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
        
        .refresh-indicator {
            position: fixed;
            top: 20px;
            right: 20px;
            background: rgba(255,255,255,0.95);
            padding: 10px 20px;
            border-radius: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            font-size: 0.9em;
            color: #666;
            z-index: 1000;
        }
        
        .learned-patterns {
            display: grid;
            gap: 10px;
        }
        
        .pattern-item {
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
            border-left: 4px solid #667eea;
        }
        
        .pattern-item strong {
            color: #667eea;
        }
        
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #999;
        }
    </style>
</head>
<body>
    <div class="refresh-indicator">
        🔄 Auto-refresh: <span id="countdown">2</span>s
    </div>
    
    <div class="container">
        <div class="header">
            <h1>🛡️ AI Agent Immune System</h1>
            <p class="subtitle">Real-time monitoring and autonomous healing</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="label">Total Agents</div>
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
            <div class="stat-card">
                <div class="label">Success Rate</div>
                <div class="value" id="stat-success">-</div>
            </div>
            <div class="stat-card">
                <div class="label">Runtime</div>
                <div class="value" id="stat-runtime">-</div>
            </div>
        </div>
        
        <div class="section">
            <h2>🤖 Agent Status Grid</h2>
            <div class="agents-grid" id="agents-grid"></div>
        </div>
        
        <div class="section">
            <h2>💊 Recent Healing Actions</h2>
            <div id="healings-list"></div>
        </div>
        
        <div class="section">
            <h2>🧠 Learned Healing Patterns</h2>
            <div class="learned-patterns" id="patterns-list"></div>
        </div>
    </div>
    
    <script>
        let countdown = 2;
        
        async function fetchData() {
            try {
                const [stats, agents, healings] = await Promise.all([
                    fetch('/api/stats').then(r => r.json()),
                    fetch('/api/agents').then(r => r.json()),
                    fetch('/api/healings').then(r => r.json())
                ]);
                
                updateStats(stats);
                updateAgents(agents);
                updateHealings(healings);
                updatePatterns(stats.learned_patterns);
            } catch (e) {
                console.error('Error fetching data:', e);
            }
        }
        
        function updateStats(stats) {
            document.getElementById('stat-agents').textContent = stats.total_agents;
            document.getElementById('stat-executions').textContent = stats.total_executions;
            document.getElementById('stat-infections').textContent = stats.total_infections;
            document.getElementById('stat-healed').textContent = stats.total_healed;
            document.getElementById('stat-success').textContent = (stats.success_rate * 100).toFixed(0) + '%';
            
            const runtime = Math.floor(stats.total_executions / stats.total_agents * 0.5);
            document.getElementById('stat-runtime').textContent = runtime + 's';
            
            const status = document.querySelector('.header .subtitle');
            if (stats.baselines_learned > 0) {
                status.textContent = `Real-time monitoring and autonomous healing • ${stats.baselines_learned} baselines learned`;
            }
        }
        
        function updateAgents(agents) {
            const grid = document.getElementById('agents-grid');
            grid.innerHTML = agents.map(agent => `
                <div class="agent-card ${agent.status}">
                    <div class="agent-id">${agent.id}</div>
                    <div class="agent-type">${agent.type}</div>
                    <span class="status ${agent.status}">${agent.status.toUpperCase()}</span>
                    ${agent.infected ? `<div style="color: #f44336; font-size: 0.85em; margin-top: 5px;">⚠️ ${agent.infection_type}</div>` : ''}
                    <div class="metrics">
                        📊 ${agent.executions} executions<br>
                        ${agent.has_baseline ? '✅ Baseline learned' : '⏳ Learning...'}
                        ${agent.latest_metrics ? `<br>⏱ ${agent.latest_metrics.latency}ms | 🔤 ${agent.latest_metrics.tokens} tokens | 🔧 ${agent.latest_metrics.tools} tools` : ''}
                    </div>
                </div>
            `).join('');
        }
        
        function updateHealings(healings) {
            const list = document.getElementById('healings-list');
            if (healings.length === 0) {
                list.innerHTML = '<div class="empty-state">No healing actions yet...</div>';
                return;
            }
            
            list.innerHTML = healings.reverse().map(h => `
                <div class="healing-record ${h.success ? 'success' : 'failed'}">
                    <div>
                        <strong>${h.agent_id}</strong>: ${h.action}
                        <div style="font-size: 0.85em; color: #666; margin-top: 4px;">
                            Diagnosis: ${h.diagnosis}
                        </div>
                    </div>
                    <span class="badge ${h.success ? 'success' : 'failed'}">
                        ${h.success ? '✅ Success' : '❌ Failed'}
                    </span>
                </div>
            `).join('');
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
        
        // Auto-refresh
        setInterval(() => {
            countdown--;
            document.getElementById('countdown').textContent = countdown;
            
            if (countdown <= 0) {
                fetchData();
                countdown = 2;
            }
        }, 1000);
        
        // Initial load
        fetchData();
    </script>
</body>
</html>
"""
