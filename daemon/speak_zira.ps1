Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice("Microsoft Zira Desktop")
$s.SetOutputToDefaultAudioDevice()
$s.Speak("Hello! I am Zira, your Blank Slate assistant. How can I help you today?")
$s.Dispose()
Write-Host "Done speaking."
