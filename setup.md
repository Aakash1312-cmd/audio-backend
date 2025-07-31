#!/bin/bash

echo "Setting up Python backend..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload

echo "Setting up React frontend..."
cd client
npm install

echo "Setup complete!"
