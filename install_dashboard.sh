#!/bin/bash
# Install Web Dashboard Dependencies

echo "🌐 Installing Web Dashboard dependencies..."
echo ""

# Navigate to project directory
cd ~/workspace/appd/hackathon

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install Flask and Flask-CORS
pip install flask flask-cors

echo ""
echo "✅ Installation complete!"
echo ""
echo "To run the demo with web dashboard:"
echo "  python3 demo.py"
echo ""
echo "Then open: http://localhost:8090"
echo ""
