import subprocess, time

# Method 1: cmd /c start with shell:AppsFolder
uwp_apps = {
    'Calculator': 'Microsoft.WindowsCalculator_8wekyb3d8bbwe!App',
    'Paint': 'Microsoft.Paint_8wekyb3d8bbwe!App',
}

for name, aumid in uwp_apps.items():
    try:
        subprocess.Popen(['cmd', '/c', 'start', '', f'shell:AppsFolder\\{aumid}'])
        print(f'shell:AppsFolder {name:15} => OK')
    except Exception as e:
        print(f'shell:AppsFolder {name:15} => {e}')
    time.sleep(1)

# Method 2: cmd /c start with just the name
more_tests = ['Calculator', 'Spotify', 'Discord', 'Steam']
for name in more_tests:
    try:
        subprocess.Popen(['cmd', '/c', 'start', '', name])
        print(f'cmd start {name:15} => OK')
    except Exception as e:
        print(f'cmd start {name:15} => {e}')
    time.sleep(1)
