#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$PythonVersion = "3.12.10"
$EmbedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"

$DistName = "NeTvoyoDelo-Local"
$DistRoot = Join-Path $Root "release\$DistName"
$CacheDir = Join-Path $Root "release\.cache"
$ZipOut = Join-Path $Root "release\$DistName.zip"

Write-Host "==> Clean $DistRoot"
if (Test-Path $DistRoot) { Remove-Item -Recurse -Force $DistRoot }
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DistRoot "python") | Out-Null

$EmbedZip = Join-Path $CacheDir "python-embed-$PythonVersion.zip"
if (-not (Test-Path $EmbedZip)) {
    Write-Host "==> Download Python embeddable $PythonVersion"
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip -UseBasicParsing
}

Write-Host "==> Extract Python"
Expand-Archive -Path $EmbedZip -DestinationPath (Join-Path $DistRoot "python") -Force

$Pth = Get-ChildItem (Join-Path $DistRoot "python") -Filter "*._pth" | Select-Object -First 1
if (-not $Pth) { throw "python*._pth not found" }
@(
    "python312.zip"
    "."
    "Lib\site-packages"
    "import site"
) | Set-Content -Path $Pth.FullName -Encoding ASCII

$Py = Join-Path $DistRoot "python\python.exe"
$GetPip = Join-Path $CacheDir "get-pip.py"
if (-not (Test-Path $GetPip)) {
    Write-Host "==> Download get-pip.py"
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPip -UseBasicParsing
}

Write-Host "==> Install pip"
& $Py $GetPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip failed" }

Write-Host "==> Install requirements"
& $Py -m pip install --no-warn-script-location -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "==> Copy application"
$AppDst = Join-Path $DistRoot "app"
if (Test-Path $AppDst) { Remove-Item -Recurse -Force $AppDst }
Copy-Item -Recurse -Force (Join-Path $Root "app") $AppDst
Get-ChildItem -Path $AppDst -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $AppDst -Recurse -Filter "*.pyc" |
    Remove-Item -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $AppDst "static\uploads") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $AppDst "static\avatars") | Out-Null

Copy-Item (Join-Path $Root "run_local.py") (Join-Path $DistRoot "run_local.py")
Copy-Item (Join-Path $Root "requirements.txt") (Join-Path $DistRoot "requirements.txt")
Copy-Item (Join-Path $Root ".env.example") (Join-Path $DistRoot ".env.example")
Copy-Item (Join-Path $Root "portable\README.txt") (Join-Path $DistRoot "README.txt")
Copy-Item (Join-Path $Root "portable\START.bat") (Join-Path $DistRoot "START.bat")
Copy-Item (Join-Path $Root "portable\STOP.bat") (Join-Path $DistRoot "STOP.bat")

# Never copy a developer .env into the distro — secrets would leak into zip/git.
$EnvDst = Join-Path $DistRoot ".env"
Copy-Item (Join-Path $Root ".env.example") $EnvDst -Force

function Set-EnvKey([string]$path, [string]$key, [string]$value) {
    $lines = @()
    if (Test-Path $path) {
        $lines = Get-Content $path -Encoding UTF8
    }
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match ("^\s*" + [regex]::Escape($key) + "\s*=")) {
            $found = $true
            "$key=$value"
        } else {
            $line
        }
    }
    if (-not $found) {
        $out = @($out) + "$key=$value"
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($path, $out, $utf8)
}

Set-EnvKey $EnvDst "HOST" "127.0.0.1"
Set-EnvKey $EnvDst "PORT" "8000"
Set-EnvKey $EnvDst "PUBLIC_BASE_URL" "http://127.0.0.1:8000"
Set-EnvKey $EnvDst "CORS_ORIGINS" "http://127.0.0.1:8000,http://localhost:8000"
Set-EnvKey $EnvDst "SEED_DEFAULT_DEPARTMENTS" "false"
Set-EnvKey $EnvDst "ADMIN_USERNAME" "admin"
Set-EnvKey $EnvDst "ADMIN_PASSWORD" "admin123"
Set-EnvKey $EnvDst "ADMIN_EMAIL" "admin@example.com"

Write-Host "==> Prepare clean database (admin only)"
$VenvPy = Join-Path $Root "venv\Scripts\python.exe"
$PrepPy = if (Test-Path $VenvPy) { $VenvPy } else { "python" }
$env:APP_ROOT = $DistRoot
$env:SEED_DEFAULT_DEPARTMENTS = "false"
& $PrepPy (Join-Path $Root "scripts\prepare_clean_db.py")
if ($LASTEXITCODE -ne 0) { throw "prepare_clean_db failed" }
Remove-Item Env:APP_ROOT -ErrorAction SilentlyContinue

Write-Host "==> ZIP"
if (Test-Path $ZipOut) { Remove-Item $ZipOut -Force }
Compress-Archive -Path $DistRoot -DestinationPath $ZipOut -Force

$size = [math]::Round((Get-Item $ZipOut).Length / 1MB, 1)
Write-Host ""
Write-Host "DONE"
Write-Host "  Folder: $DistRoot"
Write-Host "  Zip:    $ZipOut ($size MB)"
