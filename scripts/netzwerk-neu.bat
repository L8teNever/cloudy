@echo off
rem Setzt die Netzwerk-Einstellungen dieses PCs zurueck und holt neue Adressen per DHCP.
rem Nutzen: nach dem Umschalten (cloudy dhcp an/aus, cloudy gateway an/aus) einfach doppelklicken.
rem Fragt selbst nach Administratorrechten.

net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)

echo.
echo === 1/4  IP und DNS auf automatisch (DHCP) stellen ===
powershell -NoProfile -Command "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.ifIndex -Dhcp Enabled -ErrorAction SilentlyContinue; Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ResetServerAddresses -ErrorAction SilentlyContinue; Write-Host ('  ' + $_.Name) }"

echo.
echo === 2/4  Alte Adresse abgeben und neue holen ===
ipconfig /release >nul
ipconfig /renew

echo.
echo === 3/4  Zwischenspeicher leeren ===
ipconfig /flushdns >nul
arp -d * >nul 2>&1
nbtstat -R >nul 2>&1

echo.
echo === 4/4  Ergebnis ===
echo Gateway, DNS-Server und DHCP-Server sollten die IP vom Pi sein:
echo.
ipconfig /all | findstr /R /C:"Standardgateway" /C:"Default Gateway" /C:"DNS-Server" /C:"DNS Servers" /C:"DHCP-Server" /C:"DHCP Server" /C:"IPv4-Adresse" /C:"IPv4 Address"

echo.
echo Fertig. Falls noch die alten Werte stehen: WLAN kurz aus/an oder Kabel ziehen.
pause
