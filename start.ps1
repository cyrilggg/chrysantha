# Chrysantha — 一键启动脚本
# ============================
# 用法:
#   .\start.ps1              # 启动全部服务
#   .\start.ps1 dev           # 开发模式 (postgres + redis only)
#   .\start.ps1 stop          # 停止所有服务
#   .\start.ps1 sync          # 手动触发一次 A 股行情同步
#   .\start.ps1 build         # 重新编译前端并构建自定义镜像
#   .\start.ps1 logs          # 查看所有容器日志
#   .\start.ps1 status        # 查看服务状态

param(
    [ValidateSet("", "dev", "stop", "sync", "build", "logs", "status", "restart")]
    [string]$Command = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# 确保 Docker 在运行
function Ensure-Docker {
    $info = docker info 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] Docker Desktop 未运行，正在启动..." -ForegroundColor Yellow
        Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        Write-Host "等待 Docker 就绪..." -ForegroundColor Yellow
        $timeout = 60
        while ($timeout -gt 0) {
            Start-Sleep -Seconds 2
            docker info 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
            $timeout -= 2
        }
        if ($timeout -le 0) {
            Write-Host "[X] Docker 启动超时，请手动打开 Docker Desktop" -ForegroundColor Red
            exit 1
        }
    }
    Write-Host "[OK] Docker 已就绪" -ForegroundColor Green
}

function Start-All {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  Chrysantha — 统一资产看板" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""

    Ensure-Docker

    Write-Host "[1/2] 启动核心服务 (Ghostfolio + PostgreSQL + Redis)..." -ForegroundColor Yellow
    docker compose -f docker/docker-compose.yml up -d ghostfolio postgres redis
    if ($LASTEXITCODE -ne 0) { throw "启动失败" }

    Write-Host "[2/2] 等待服务健康检查..." -ForegroundColor Yellow
    $timeout = 30
    while ($timeout -gt 0) {
        try {
            $health = Invoke-WebRequest -Uri "http://localhost:3333/api/v1/health" -UseBasicParsing -TimeoutSec 3
            if ($health.Content -match "OK") { break }
        } catch {}
        Start-Sleep -Seconds 2
        $timeout -= 2
    }

    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "  Ghostfolio 已启动!" -ForegroundColor Green
    Write-Host "  打开浏览器访问: http://localhost:3333" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "快速操作:" -ForegroundColor White
    Write-Host "  .\start.ps1 sync    — 手动同步 A 股行情" -ForegroundColor Gray
    Write-Host "  .\start.ps1 build   — 编译自定义前端" -ForegroundColor Gray
    Write-Host "  .\start.ps1 logs    — 查看日志" -ForegroundColor Gray
    Write-Host "  .\start.ps1 stop    — 停止服务" -ForegroundColor Gray
}

function Start-Dev {
    Ensure-Docker
    Write-Host "[DEV] 启动开发环境 (PostgreSQL + Redis)..." -ForegroundColor Yellow
    docker compose -f docker/docker-compose.dev.yml up -d
    Write-Host "[OK] 开发环境已启动" -ForegroundColor Green
    Write-Host "  PostgreSQL: localhost:5432" -ForegroundColor Gray
    Write-Host "  Redis:      localhost:6379" -ForegroundColor Gray
    Write-Host ""
    Write-Host "然后运行:" -ForegroundColor White
    Write-Host "  npm run start:server   — 启动 API 服务" -ForegroundColor Gray
    Write-Host "  npm run start:client   — 启动前端开发服务器" -ForegroundColor Gray
}

function Stop-All {
    Write-Host "停止所有服务..." -ForegroundColor Yellow
    docker compose -f docker/docker-compose.yml down
    docker compose -f docker/docker-compose.dev.yml down 2>$null
    Write-Host "[OK] 所有服务已停止" -ForegroundColor Green
}

function Invoke-Sync {
    Write-Host "手动触发 A 股行情同步..." -ForegroundColor Yellow
    Write-Host "确保已在 .env 中设置了 GHOSTFOLIO_ACCESS_TOKEN"
    Write-Host "确保已在 .env 中设置了 CHRYSANTHA_SYMBOLS (如: CHRYSANTHA_SYMBOLS=SH600519,SZ002594)"
    Write-Host ""
    docker compose -f docker/docker-compose.yml run --rm chrysantha-sync python sync.py
}

function Invoke-Build {
    Write-Host "重新编译前端..." -ForegroundColor Yellow
    Write-Host "这可能需要几分钟..." -ForegroundColor Gray
    
    # 刷新 PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    & "C:\Program Files\nodejs\npm.cmd" run build:production
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[X] 编译失败" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] 编译完成" -ForegroundColor Green
}

function Show-Logs {
    docker compose -f docker/docker-compose.yml logs --tail 50 -f
}

function Show-Status {
    Write-Host ""
    Write-Host "=== 服务状态 ===" -ForegroundColor Cyan
    docker compose -f docker/docker-compose.yml ps
    Write-Host ""
    try {
        $health = Invoke-WebRequest -Uri "http://localhost:3333/api/v1/health" -UseBasicParsing -TimeoutSec 3
        Write-Host "Ghostfolio API: $($health.Content)" -ForegroundColor Green
    } catch {
        Write-Host "Ghostfolio API: 不可达" -ForegroundColor Red
    }
}

function Restart-All {
    Stop-All
    Start-All
}

# ── Main ──

switch ($Command) {
    "dev"     { Start-Dev }
    "stop"    { Stop-All }
    "sync"    { Invoke-Sync }
    "build"   { Invoke-Build }
    "logs"    { Show-Logs }
    "status"  { Show-Status }
    "restart" { Restart-All }
    default   { Start-All }
}
