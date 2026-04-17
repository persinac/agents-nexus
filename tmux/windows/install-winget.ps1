# Source - https://stackoverflow.com/a/79784564
# Posted by Robert Gibson, modified by community. See post 'Timeline' for change history
# Retrieved 2026-03-28, License - CC BY-SA 4.0

Set-PSRepository -N 'PSGallery' -InstallationPolicy Trusted
Install-Script -Name winget-install -Force
winget-install.ps1
