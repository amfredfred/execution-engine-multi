# Quick Start Guide - Windows NSSM

Get the Execution Engine running as a Windows service in 5 minutes.

## Prerequisites

- Windows 7 or later
- Python 3.12+
- MetaTrader 5 terminal (running)
- Administrator privileges

## Step 1: Clone Repository

```powershell
git clone https://github.com/amfredfred/bobis-quote-mt5-trading-engine-py.git
cd execution-engine
```

## Step 2: Setup Python Environment

```powershell
# Create virtual environment
python -m venv venv

# Activate it
venv\Scripts\activate

# Install dependencies
pip install -e .
```

## Step 3: Configure

```powershell
# Copy example configuration
Copy-Item .env.example .env

# Edit .env
notepad .env
```

**Required fields**:
```dotenv
MT5_LOGIN=your_login_number
MT5_PASSWORD=your_password
MT5_SERVER=your_server_address
```

**Key risk settings** (set these before going live):
```dotenv
MAX_LOSING_STREAK=4          # Your system's worst recorded consecutive losing streak
                             # Determines max concurrent trades (streak + 1) and
                             # per-trade risk amount automatically
MAX_DAILY_LOSS_PERCENT=5.0   # Daily loss budget as % of account equity
SL_RATIO_THRESHOLD=0.34      # Max spread/SL ratio — lower = stricter
MIN_RR_RATIO=1.0             # Minimum risk:reward ratio
```

## Step 4: Validate Configuration

```powershell
python scripts/check_env.py
```

Should output: `OK: .env looks good`

If you see `ValueError: MAX_LOSING_STREAK must be >= 1` — set `MAX_LOSING_STREAK` to at least `1` in `.env` and re-run.

## Step 5: Install as Service

**Run PowerShell as Administrator**, then:

```powershell
cd execution-engine
powershell -ExecutionPolicy Bypass -File install_service.ps1
```

The script will:
- Download NSSM if needed
- Register the service
- Set it to start automatically
- Start it now

✓ Done!

## Verify Installation

```powershell
# Check status
powershell -File scripts/service.ps1 status

# View logs
powershell -File scripts/service.ps1 logs

# Should see something like:
# Service Status: SERVICE_RUNNING
```

## Common Operations

```powershell
# View status
powershell -File scripts/service.ps1 status

# Watch logs in real-time
powershell -File scripts/service.ps1 logs

# Restart service
powershell -File scripts/service.ps1 restart

# Stop service
powershell -File scripts/service.ps1 stop

# Start service again
powershell -File scripts/service.ps1 start

# Remove service (keep code, just unregister)
powershell -File scripts/service.ps1 remove
```

## View Logs

### Real-time Monitoring
```powershell
powershell -File scripts/service.ps1 logs
```

### Error Log File
```powershell
Get-Content logs\service_stderr.log -Tail 50

# Or open directly
notepad logs\service_stderr.log
```

### Windows Event Viewer
1. Open Event Viewer (eventvwr.msc)
2. Navigate to `Windows Logs > Application`
3. Look for ExecutionEngine events

## Troubleshooting

### Service won't start

Check error log:
```powershell
Get-Content logs\service_stderr.log -Tail 100 | Out-Host
```

Common causes:
- `.env` file missing or invalid
- MT5 terminal not running
- `MAX_LOSING_STREAK` missing or set to `0` (must be >= 1)
- Python environment not activated before install

**Solution**:
```powershell
# Reinstall service
powershell -File scripts/service.ps1 remove
powershell -File scripts/install_service.ps1
```

### High CPU/Memory Usage

Check if running normally:
```powershell
Get-Process python | Select-Object Name, Handles, WorkingSet
```

For memory issues, restart daily:
```powershell
# Scheduled restart at 2 AM (Windows Task Scheduler)
# See docs/deployment.md for details
```

### Can't run PowerShell scripts

Enable execution policy:
```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope CurrentUser
```

### Need to update .env

1. Stop service:
   ```powershell
   powershell -File scripts/service.ps1 stop
   ```

2. Edit .env:
   ```powershell
   notepad .env
   ```

3. Restart service:
   ```powershell
   powershell -File scripts/service.ps1 start
   ```

## Update/Upgrade

```powershell
cd execution-engine

# Pull latest code
git pull

# Activate environment
venv\Scripts\activate

# Update dependencies
pip install -e . --upgrade

# Restart service
powershell -File scripts/service.ps1 restart
```

## Backup

Backup your database and configuration:
```powershell
powershell -File scripts/backup.ps1
```

Creates timestamped backup in `backups/` directory.

## Advanced: Manual Service Commands

Using NSSM directly:

```powershell
# Check status
nssm status ExecutionEngine

# View configuration
nssm dump ExecutionEngine

# Edit config (GUI)
nssm edit ExecutionEngine

# Manual start
nssm start ExecutionEngine

# Manual stop
nssm stop ExecutionEngine confirm

# Manual restart
nssm restart ExecutionEngine
```

## Get Help

- 📖 [Full Documentation](../docs/)
- 🐛 [Report Issues](https://github.com/amfredfred/execution-engine/issues)
- 💬 [Discussions](https://github.com/amfredfred/execution-engine/discussions)
- 📋 [Deployment Guide](../docs/deployment.md)

## Next Steps

1. **Monitor Dashboard**: Run the separate `execution-engine-dashboard` app and connect it to `ws://localhost:8080/ws`
2. **Send Signals**: Use the dashboard/WebSocket bridge at `ws://localhost:8080/ws`
3. **Review Risk Settings**: Tune `MAX_LOSING_STREAK`, `MAX_DAILY_LOSS_PERCENT`, and `SL_RATIO_THRESHOLD` in `.env`
4. **Review Logs**: Check logs regularly for rule rejections and sizing info
5. **Test Signals**: Start with demo account to verify integration before live trading

## Security Reminders

- ✓ Keep `.env` file private (git-ignored)
- ✓ Don't share your MT5 credentials
- ✓ Rotate passwords regularly
- ✓ Use firewall to restrict WebSocket access
- ✓ Keep Windows and Python updated

---

**Ready to trade?** Your Execution Engine is now running as a Windows service! 🚀
