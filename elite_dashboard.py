#!/bin/bash
# VPS Deployment Commands for Fixed Elite Dashboard
# Run these commands on your VPS at /opt/elite-scanner

cd /opt/elite-scanner

echo "=== BACKUP CURRENT DASHBOARD ==="
cp elite_dashboard.py "elite_dashboard_BACKUP_BEFORE_FIX_$(date +%Y%m%d_%H%M%S).py"

echo "=== REPLACE WITH FIXED DASHBOARD ==="
# Upload the fixed elite_dashboard.py to GitHub first, then:
git fetch origin
git reset --hard origin/main

echo "=== COMPILE CHECK ==="
/opt/elite-scanner/.venv/bin/python -m py_compile elite_dashboard.py && echo "✓ Compile: PASS" || echo "✗ Compile: FAIL"

echo ""
echo "=== VERIFICATION ==="
grep -n "Signal Desk Details" elite_dashboard.py && echo "Found in code (should only be comment)" || echo "✓ Signal Desk Details removed"
grep -n "PLUG" elite_dashboard.py && echo "✗ PLUG wording exists" || echo "✓ PLUG wording removed"
grep -n "if not priority_signals:" elite_dashboard.py && echo "✗ Hide-if-empty bug exists" || echo "✓ Hide-if-empty bug removed"
grep -n "Priority Morning Reclaim stays visible here" elite_dashboard.py | head -2
grep -n "FIRST_REGULAR_SCANNER_MINUTE = 40" elite_dashboard.py

echo ""
echo "=== REBUILD DASHBOARD ==="
/opt/elite-scanner/.venv/bin/python elite_runner.py --once-dashboard

echo ""
echo "=== CHECK LIVE HTML ==="
grep -n "Signal Desk Details" dashboard.html /var/www/elite-scanner/index.html && echo "✗ Signal Desk Details in HTML" || echo "✓ Signal Desk Details NOT in HTML"
grep -n "Priority Morning Reclaim" dashboard.html /var/www/elite-scanner/index.html | head -5
grep -n "PLUG" dashboard.html /var/www/elite-scanner/index.html && echo "✗ PLUG in HTML" || echo "✓ PLUG NOT in HTML"

echo ""
echo "=== RESTART SERVICE ==="
systemctl restart elite-scanner.service
sleep 5
systemctl status elite-scanner.service --no-pager -l

echo ""
echo "=== DEPLOYMENT COMPLETE ==="
echo "Open dashboard with cache buster: http://178.105.172.89/?v=$(date +%s)"
