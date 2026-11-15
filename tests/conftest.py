"""Pytest configuration: put the repo root on sys.path. No test here
touches the live Fyers API or Telegram; all broker calls use fakes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
