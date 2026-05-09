#!/bin/bash
cd /mnt/c/Users/Admin/Desktop/QQQ_Live
PYTHONUNBUFFERED=1 python live_trader.py >> /tmp/qqq_live_output.log 2>&1
