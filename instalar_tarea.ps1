# Registra Kettenblick como tarea programada de Windows.
# Se ejecuta al iniciar sesión y se reinicia sola si el proceso muere.
# No necesita permisos de administrador para el usuario actual.

$ErrorActionPreference = 'Stop'

$directorio = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python.exe -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$script = Join-Path $directorio 'servicio_kettenblick.py'
$nombre = 'Kettenblick - Capturador continuo'

Write-Host "Directorio: $directorio"
Write-Host "Python:     $pythonw"
Write-Host "Script:     $script"

$accion = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "`"$script`"" `
    -WorkingDirectory $directorio

$disparador = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$ajustes = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $nombre `
    -Action $accion `
    -Trigger $disparador `
    -Settings $ajustes `
    -Description 'Mantiene Streamlit + worker de Kettenblick activos y capturando cada 5 min.' `
    -Force | Out-Null

Write-Host ""
Write-Host "Tarea creada: $nombre"
Write-Host "Arrancar ahora:  Start-ScheduledTask -TaskName '$nombre'"
Write-Host "Ver estado:      Get-ScheduledTask -TaskName '$nombre' | Get-ScheduledTaskInfo"
Write-Host "Quitar tarea:    Unregister-ScheduledTask -TaskName '$nombre' -Confirm:`$false"
