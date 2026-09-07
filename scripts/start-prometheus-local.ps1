# 本地（无 Docker）启动 Prometheus：
# - 抓取本机 uvicorn 的 /metrics（deploy/prometheus/prometheus.local.yml）
# - 数据目录在仓库外，保留 1 年；停止后用 Stop-Process 结束 prometheus 进程即可。
param(
    [string]$PrometheusExe = "D:\Code\tools\prometheus-2.53.0.windows-amd64\prometheus.exe",
    [string]$DataPath = "D:\Code\tools\prometheus-local-data"
)

$config = Join-Path $PSScriptRoot "..\deploy\prometheus\prometheus.local.yml"
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
Start-Process -FilePath $PrometheusExe -WindowStyle Hidden -ArgumentList @(
    "--config.file=$config",
    "--storage.tsdb.path=$DataPath",
    "--storage.tsdb.retention.time=1y",
    "--web.listen-address=127.0.0.1:9090"
)
Write-Host "Prometheus started at http://127.0.0.1:9090 (data: $DataPath)"
