#!/bin/bash
python main.py &
cd webapp && gunicorn -w 2 -b 0.0.0.0:$PORT app:app
