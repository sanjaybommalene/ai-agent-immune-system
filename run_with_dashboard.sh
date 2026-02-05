#!/bin/bash
# Run AI Agent Immune System with Web Dashboard

echo "🛡️  Starting AI Agent Immune System with Web Dashboard..."
echo ""

cd ~/workspace/appd/hackathon
source venv/bin/activate

echo "Dashboard will be available at: http://localhost:8090"
echo "Press Ctrl+C to stop"
echo ""

python3 demo.py
