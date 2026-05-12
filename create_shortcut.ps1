$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("C:\Users\kevin\Desktop\SchwabLiveTrader.lnk")
$s.TargetPath = "C:\Users\kevin\schwab-momentum-bot\dist\SchwabLiveTrader\SchwabLiveTrader.exe"
$s.WorkingDirectory = "C:\Users\kevin\schwab-momentum-bot"
$s.IconLocation = "C:\Users\kevin\schwab-momentum-bot\dist\SchwabLiveTrader\SchwabLiveTrader.exe,0"
$s.Description = "Schwab Live Trader - Automated Trading Bot"
$s.Save()
Write-Host "Shortcut created on Desktop"
