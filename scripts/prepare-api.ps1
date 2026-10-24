$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$backendPath = Join-Path $PSScriptRoot "..\backend"
$venvPath = Join-Path $backendPath ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $backendPath "requirements.txt"
$envPath = Join-Path $backendPath ".env"

function Get-PythonForVenv {
    foreach ($version in @("3.12", "3.11", "3.10", "3.13")) {
        & py "-$version" -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $version
        }
    }
    return $null
}

if (-not (Test-Path $pythonPath)) {
    Write-Host "Creating the backend Python environment..."
    $pythonVersion = Get-PythonForVenv
    if ($pythonVersion) {
        Write-Host "Using Python $pythonVersion (TensorFlow does not support Python 3.14 yet)."
        & py "-$pythonVersion" -m venv $venvPath
    } else {
        & py -3 -m venv $venvPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create a Python environment. Install Python 3.10-3.13 and the Windows Python launcher."
    }
}

& $pythonPath -c "import fastapi, dotenv, jwt, bcrypt, cryptography, multipart, cv2, deepface, faster_whisper, tensorflow, tf_keras"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing backend requirements..."
    $pipInstalled = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-Host "Pip install attempt $attempt of 3..."
        & $pythonPath -m pip install --default-timeout 120 --retries 10 -r $requirementsPath
        if ($LASTEXITCODE -eq 0) {
            $pipInstalled = $true
            break
        }
        Write-Host "Pip install attempt $attempt failed. Retrying..."
        Start-Sleep -Seconds 5
    }
    if (-not $pipInstalled) {
        throw "Backend dependencies could not be installed. Check your network and Python installation."
    }
}

if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $backendPath ".env.example") $envPath
}

$envText = Get-Content $envPath -Raw
if ($envText.Contains("replace-with-a-long-random-secret") -or $envText.Contains("replace-with-base64-encoded-32-byte-key")) {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $jwtBytes = New-Object byte[] 48
    $fileKeyBytes = New-Object byte[] 32
    $rng.GetBytes($jwtBytes)
    $rng.GetBytes($fileKeyBytes)
    $jwtSecret = [Convert]::ToBase64String($jwtBytes)
    $fileKey = [Convert]::ToBase64String($fileKeyBytes)
    $rng.Dispose()
    $envText = $envText.Replace("replace-with-a-long-random-secret", $jwtSecret)
    $envText = $envText.Replace("replace-with-base64-encoded-32-byte-key", $fileKey)
    Set-Content -Path $envPath -Value $envText -Encoding ASCII
    Write-Host "Generated local development secrets in backend\.env. Keep this file private."
}
