# Deployment Guide

This guide covers deploying the Execution Engine for production use on Windows and Linux systems.

## Quick Start

Choose your deployment method:

- **Windows Native (Recommended)**: [NSSM Service](#windows-nssm-service)
- **Cross-Platform**: [Docker](#docker-deployment)
- **Linux/Mac**: [Systemd Service](#linux-systemd-service)

## Windows NSSM Service

NSSM (Non-Sucking Service Manager) is the **recommended approach for Windows** because:
- Native Windows service integration
- Direct MetaTrader 5 terminal access
- Automatic restart on failure
- Windows Event Viewer logging
- No container overhead
- 24/7 operation optimized for trading

### Prerequisites

- Windows 7 or later
- Administrator privileges
- MetaTrader 5 terminal installed and running
- Python 3.12+
- Virtual environment setup

### Installation

1. **Prepare environment**:
   ```powershell
   # Create virtual environment
   python -m venv venv
   venv\Scripts\activate

   # Install dependencies
   pip install -e .[dev]

   # Validate configuration
   python scripts/check_env.py
   ```

2. **Create logs directory**:
   ```powershell
   New-Item -ItemType Directory -Force -Path logs
   ```

3. **Install as service** (run as Administrator):
   ```powershell
   powershell -ExecutionPolicy Bypass -File install_service.ps1
   ```

   The script will:
   - Download NSSM if needed
   - Register ExecutionEngine service
   - Set automatic startup
   - Configure auto-restart on failure
   - Start the service

4. **Verify installation**:
   ```powershell
   # Check service status
   nssm status ExecutionEngine

   # View logs
   Get-Content logs\service_stderr.log -Tail 50 -Wait

   # Windows Services: services.msc
   ```

### Service Management

```powershell
# Check status
nssm status ExecutionEngine

# Start service
nssm start ExecutionEngine

# Stop service
nssm stop ExecutionEngine

# Restart service
nssm restart ExecutionEngine

# Edit service configuration
nssm edit ExecutionEngine

# Uninstall service
powershell -ExecutionPolicy Bypass -File install_service.ps1 -Action uninstall
```

### Monitoring

**Windows Event Viewer**:
1. Open `Event Viewer`
2. Go to `Windows Logs > Application`
3. Look for `ExecutionEngine` events

**Service Logs**:
```powershell
# Real-time monitoring
Get-Content logs\service_stderr.log -Tail 50 -Wait

# Check service startup log
Get-Content logs\service_stdout.log
```

### Troubleshooting

**Service won't start**:
```powershell
# Check error log
Get-Content logs\service_stderr.log -Tail 100

# Verify .env file exists and is valid
type .env

# Test Python executable
venv\Scripts\python -m src --help
```

**Permission denied**:
```powershell
# Must run PowerShell as Administrator
Start-Process powershell -Verb RunAs
```

**NSSM issues**:
```powershell
# Manually remove service
nssm remove ExecutionEngine confirm

# Reinstall
powershell -ExecutionPolicy Bypass -File install_service.ps1
```

**`MAX_LOSING_STREAK` validation error on startup**:
```
ValueError: MAX_LOSING_STREAK must be >= 1
```
Set `MAX_LOSING_STREAK` to your system's worst recorded consecutive losing streak (minimum `1`) in `.env` and restart the service.

---

## Docker Deployment

Docker is useful for:
- Cross-platform deployments (Windows, Linux, Mac)
- Cloud infrastructure (AWS, Azure, GCP)
- Container orchestration (Kubernetes)
- Containerized development/testing

### Prerequisites

- Docker Desktop installed
- Docker CLI available
- MetaTrader 5 terminal access (requires special setup)

### Build Image

```bash
# Build image
docker build -t execution-engine:latest .

# Tag for registry
docker tag execution-engine:latest amfredfred/execution-engine:latest
```

### Run Container

**Development**:
```bash
docker run -it \
  --env-file .env \
  -p 8080:8080 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  execution-engine:latest
```

**Production**:
```bash
docker run -d \
  --name execution-engine \
  --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -v execution-engine-data:/app/data \
  -v execution-engine-logs:/app/logs \
  execution-engine:latest
```

### Docker Compose

```bash
# Start services
docker-compose up -d

# View logs
docker-compose logs -f execution-engine

# Stop services
docker-compose down

# Remove volumes
docker-compose down -v
```

### MT5 with Docker

**Note**: MT5 terminal typically runs on Windows host, not in container.

**Options**:
1. Run engine in Docker, connect to host MT5 (requires network setup)
2. Run everything on Windows with NSSM (recommended for trading)
3. Use cloud MT5 broker with API access

**Host Network Access**:
```bash
# On Windows with Docker Desktop
docker run -it \
  --env-file .env \
  -e MT5_HOST=host.docker.internal \
  execution-engine:latest
```

---

## Linux Systemd Service

Deploy on Linux/Mac with systemd:

### Prerequisites

- Linux system with systemd
- Python 3.12+
- systemctl available

### Installation

1. **Prepare environment**:
   ```bash
   git clone https://github.com/amfredfred/bobis-quote-mt5-trading-engine-py.git
   cd execution-engine

   python3 -m venv venv
   source venv/bin/activate
   pip install -e .

   cp .env.example .env
   # Edit .env with your settings
   ```

2. **Create systemd service** (`/etc/systemd/system/execution-engine.service`):
   ```ini
   [Unit]
   Description=Execution Engine - Trade Execution Engine for MetaTrader 5
   After=network.target

   [Service]
   Type=simple
   User=trading
   WorkingDirectory=/home/trading/execution-engine
   Environment="PATH=/home/trading/execution-engine/venv/bin"
   EnvironmentFile=/home/trading/execution-engine/.env
   ExecStart=/home/trading/execution-engine/venv/bin/python -m src
   Restart=on-failure
   RestartSec=10
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```

3. **Enable and start**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable execution-engine
   sudo systemctl start execution-engine
   ```

4. **Monitor**:
   ```bash
   # Status
   systemctl status execution-engine

   # Logs
   journalctl -u execution-engine -f

   # Full logs
   journalctl -u execution-engine --since "1 hour ago"
   ```

---

## Dashboard Monitoring

The execution engine exposes a WebSocket UI bridge on the configured monitoring
port. Use the separate `execution-engine-dashboard` app for live state,
metrics, risk guards, rejections, and dashboard commands.

```text
ws://localhost:8080/ws
```

---

## Backup & Recovery

### Database Backups

```powershell
# Windows - backup every 6 hours
$schedule = New-Object -TypeName Microsoft.Win32.TaskScheduler.TaskDefinition
$task = Register-ScheduledTask `
  -TaskName "ExecutionEngine-Backup" `
  -Action (New-ScheduledTaskAction -Execute "powershell" -Argument "-File backup.ps1") `
  -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 6)) `
  -RunLevel Highest
```

### Manual Backup

```bash
# Backup database
cp data/engine.db data/engine.db.backup

# Backup configuration
cp .env .env.backup

# Backup logs
tar czf logs-$(date +%Y%m%d).tar.gz logs/
```

---

## Performance Tuning

### Memory Usage

Monitor memory in Task Manager or with:
```powershell
Get-Process python | Select-Object Name, WorkingSet
```

If memory grows unboundedly:
- Check for event bus memory leaks
- Verify database WAL cleanup
- Monitor queue sizes in logs

### CPU Usage

Monitor CPU in Task Manager or with:
```powershell
Get-Process python | Select-Object Name, CPU
```

If CPU is consistently high:
- Review signal processing rate
- Check for tight loops in rules
- Monitor database query performance

---

## Scaling

### Multiple Instances

For high-frequency strategies, consider:
1. Separate strategies per service instance
2. Load balancing signal routing
3. Shared database for state

### Resource Limits (Docker)

```yaml
services:
  execution-engine:
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.5'
          memory: 256M
```

---

## Security

### Service Isolation

- Run service as low-privilege user (not Administrator)
- Use firewall rules to restrict WebSocket access
- Enable Windows Defender/antivirus

### Configuration Security

- Use environment variables for secrets
- Rotate MT5 credentials regularly
- Enable Windows Credential Manager for passwords
- Use VPN for remote connections

### Log Security

- Rotate logs regularly
- Encrypt sensitive data in logs
- Restrict log file access permissions

---

## Disaster Recovery

### Automated Restarts

NSSM automatically restarts failed services. Verify:
```powershell
nssm get ExecutionEngine AppExit
# Should show restart settings
```

### Daily Restarts

Prevent memory leaks with scheduled restarts:

**Windows Task Scheduler**:
```powershell
$trigger = New-ScheduledTaskTrigger -Daily -At 02:00AM
Register-ScheduledTask `
  -TaskName "RestartExecutionEngine" `
  -Action (New-ScheduledTaskAction -Execute "nssm" -Argument "restart ExecutionEngine") `
  -Trigger $trigger `
  -RunLevel Highest
```

**Linux cron**:
```bash
# Daily restart at 2 AM
0 2 * * * systemctl restart execution-engine
```

Note: `LossTracker` automatically resets its daily state at midnight via the internal `paused_until` rollover mechanism. A daily service restart is optional but recommended to clear any in-memory accumulation.

---

## Choosing Your Deployment

| Requirement | NSSM | Docker | Systemd |
|-------------|------|--------|---------|
| **Windows native** | ✓ Best | Limited | ✗ |
| **MT5 integration** | ✓ Direct | Complex | Depends |
| **Cross-platform** | ✗ | ✓ Best | Linux only |
| **Cloud ready** | ✗ | ✓ Best | ✓ |
| **Kubernetes** | ✗ | ✓ | ✗ |
| **Simple setup** | ✓ | Moderate | ✓ |
| **24/7 trading** | ✓ Best | ✓ | ✓ |

**TL;DR**: Use **NSSM** for Windows trading, **Docker** for cloud/scaling, **systemd** for Linux.
