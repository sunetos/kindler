#!/bin/sh

workon kindler
killall kindler; nohup ./main.py &
