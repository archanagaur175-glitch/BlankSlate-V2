Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $s.GetInstalledVoices()
foreach ($v in $voices) {
    Write-Host $v.VoiceInfo.Name "-" $v.VoiceInfo.Culture
}
$s.Dispose()
