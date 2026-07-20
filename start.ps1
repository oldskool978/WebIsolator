$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
$IsAdmin = $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Host "[INFO] Requesting Administrative Elevation to configure kernel security layers..." -ForegroundColor Cyan
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Set-Location -Path $PSScriptRoot

# Execute the supervisor engine
python isolator.py --start

Write-Host "`n[SUCCESS] Startup pipeline execution completed." -ForegroundColor Green
Start-Sleep -Seconds 3