<#
.SYNOPSIS
  Deploy Dublin Transit AI to the VM: pack the repo, copy it over SSH, unpack, rebuild and restart.

.EXAMPLE
  .\deploy\deploy.ps1 -VmHost dublin-transit.swedencentral.cloudapp.azure.com

  Never ships .env, data/, virtualenvs, node_modules, .next, eval/results or .git.
  The server keeps its own ~/dublin-transit-ai/.env (created once, chmod 600) and the last 5 release tarballs
  in ~/releases for rollback (see deploy/README.md).
#>
param(
    [Parameter(Mandatory = $true)] [string] $VmHost,
    [string] $User = "azureuser",
    [string] $KeyPath = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$tarball = Join-Path $env:TEMP "dublin-transit-$stamp.tar.gz"
$remoteScript = Join-Path $env:TEMP "dublin-transit-deploy-$stamp.sh"
$target = "$User@$VmHost"
$sshArgs = @("-o", "StrictHostKeyChecking=accept-new")
if ($KeyPath) { $sshArgs += @("-i", $KeyPath) }

function Invoke-Native([string] $exe, [string[]] $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

# 1. Pack (bsdtar ships with Windows 10+). Anchored patterns (./x) only match at the repo root.
Write-Host "Packing $repo ..."
$excludes = @(
    "--exclude=./data", "--exclude=./eval/results", "--exclude=./.env",
    "--exclude=.git", "--exclude=.venv", "--exclude=node_modules", "--exclude=.next",
    "--exclude=__pycache__", "--exclude=.pytest_cache", "--exclude=.ruff_cache"
)
Invoke-Native "tar" (@("-czf", $tarball) + $excludes + @("-C", $repo, "."))
$sizeMb = [math]::Round((Get-Item $tarball).Length / 1MB, 1)
Write-Host "Package: $sizeMb MB"

# Refuse to ship anything secret, even if a pattern above was wrong.
$listing = & tar -tzf $tarball
if ($listing | Where-Object { $_ -match '(^|/)\.env$' }) { throw "refusing to deploy: the package contains a .env file" }

# 2. Server-side steps, written with LF line endings so bash can run them.
$remote = @'
set -euo pipefail
STAMP="$1"
APP=~/dublin-transit-ai
mkdir -p "$APP" ~/releases
mv ~/"dublin-transit-$STAMP.tar.gz" ~/releases/
tar -xzf ~/releases/"dublin-transit-$STAMP.tar.gz" -C "$APP"
cd "$APP"
if [ ! -f .env ]; then echo "ERROR: $APP/.env is missing (create it once, then chmod 600)"; exit 1; fi
chmod 600 .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
ls -1t ~/releases/dublin-transit-*.tar.gz | tail -n +6 | xargs -r rm --
echo "Deployed release $STAMP"
'@
[System.IO.File]::WriteAllText($remoteScript, ($remote -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))

# 3. Copy and run.
Write-Host "Copying to $target ..."
Invoke-Native "scp" ($sshArgs + @($tarball, $remoteScript, "${target}:~/"))
Write-Host "Unpacking and restarting on the VM (the first build takes several minutes) ..."
Invoke-Native "ssh" ($sshArgs + @($target, "bash ~/dublin-transit-deploy-$stamp.sh $stamp && rm ~/dublin-transit-deploy-$stamp.sh"))

Remove-Item $tarball, $remoteScript
Write-Host "Done: https://$VmHost/"
