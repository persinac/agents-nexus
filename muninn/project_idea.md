# Muninn

Project Muninn: In Norse mythology, Odin has two ravens that fly around the world gathering information. Huginn represents "thought," and Muninn represents "memory." Since this pipeline is literally taking your daily thoughts from the reMarkable and depositing them into your Obsidian vault (your second memory), Muninn is a perfect fit.

Desired workflow concepts: 
  - syncing data
  - bridging devices
  - translating handwriting
  - building a knowledge vault

Actor workflow:
  - Write notes down in digital notebook
  - Notes get sync'd to digital cloud
  - Process picks up new note(s)
  - Translates my shitty handwriting
  --- interprets my drawings
  - Pushes to S3 or similar
  - Sync's to obsidian vault(s)

Constraint: I already had the RM1

# Research (round 1)

digital note-taking workflow, you will need to rely heavily on community-developed tools or consider specific e-ink devices that align with your goals. Based on the provided sources, here is the direction you should take for each step of your requested workflow:
1. Write notes down in a digital notebook You have three main e-ink tablet ecosystems to choose from:
reMarkable 2 (rM2): Offers an excellent, distraction-free "paper-like" writing experience
. For your highly automated workflow, the rM2 is the best candidate because it grants you root SSH access by default and supports the Toltec package manager, which allows you to install custom Linux utilities
. (Note: Be cautious with the newer reMarkable Paper Pro, as its root partition is read-only and Toltec does not currently support it
).
Onyx Boox: Runs on Android, giving you access to the Google Play Store to install native apps
.
Supernote: Highly praised for its organization and offline features
.
2 & 3. Sync to cloud & Process picks up new note(s) If you go the reMarkable route, you can bypass the official cloud and set up an automated, self-hosted pipeline:
rclone: You can install rclone directly onto a reMarkable tablet using Toltec
. rclone is a powerful command-line tool that can automatically sync files using a systemd timer or cron job at specific intervals
.
Syncthing: Also available via Toltec, this provides real-time, peer-to-peer file synchronization directly to a PC or server without needing a central cloud
. However, users warn it can be resource-intensive and drain the tablet's battery
.
PC-side Processing: You can use RemarkableSync, a Python toolkit you run on your computer that connects to the tablet via USB/SSH to incrementally back up and convert new notebooks into PDFs
. Alternatively, rMAPI is a Go-based app that allows you to programmatically access the reMarkable Cloud API to pull down your files via scripts
.
4. Translates my shitty handwriting (OCR)
reMarkable: It has built-in handwriting conversion, but users note it is clunky—it must be triggered manually on a page-by-page basis and requires an active Wi-Fi connection to process the text in the cloud
. It does not do automatic background indexing
.
Supernote: Features robust offline, real-time handwriting recognition directly on the device
.
Boox: According to several users, Boox devices currently have the best and most accurate handwriting OCR among e-ink tablets
.
5. Interprets my drawings Please note: None of the provided sources contain information about software or tools that can interpret drawings. To achieve this, you would likely need to build a custom step in your pipeline that feeds your synced notebooks into an external AI vision model.
6. Pushes to S3 or similar rclone is the definitive tool for this step. It supports over 70 cloud storage providers, including Amazon S3, Google Drive, and Nextcloud
. Whether you run rclone directly on the reMarkable tablet or on a processing server that has pulled your notes, it can handle pushing your files to an S3 bucket seamlessly.
7. Sync's to Obsidian vault(s) While one user expressed frustration that there is no straightforward way to extract handwriting as SVGs and OCR text into an Obsidian vault
, other users have successfully bridged this gap:
One user mentioned bringing an old Obsidian plugin for reMarkable "back from the dead"
.
Another user specifically mentions relying on a third-party tool called scrybble.ink to bring reMarkable documents and on-the-go notes directly into Obsidian
.
Summary Recommendation: If you want to build this pipeline yourself using Linux tools (like rclone and scripts), a reMarkable 2 is your best bet due to its root access and the Toltec community
. If top-tier handwriting recognition (OCR) and an easier out-of-the-box sync experience are your main priorities, you may want to look into an Android-based Boox device or a Supernote

# Research (round 2)

reMarkable 1 and 2 share the same underlying Linux-based operating system (Codex), the vast majority of the community-developed tools built for this ecosystem treat them exactly the same
. In fact, the older devices are generally much friendlier to tinker with than the newest Paper Pro model, which locks things down behind a destructive "Developer Mode"
.
Here is how your reMarkable 1 fits perfectly into your proposed workflow:
1. Root Access and Package Management You have root SSH access to your reMarkable 1 right out of the box. You can find your SSH password by going to Settings > Help > Copyrights and Licenses
. Once you SSH into the tablet, you can easily install Toltec, the community package manager, which automatically detects your reMarkable 1 and sets up the correct display dependencies
.
2. Syncing to Cloud & Pushing to S3 Through Toltec, you can install rclone or Syncthing directly onto your tablet
.
rclone can be set up with a cron job to automatically push your raw notebooks or exported PDFs directly to an Amazon S3 bucket on a schedule
.
rmfakecloud: If you want to completely bypass reMarkable's official cloud but still want an automated "cloud" experience, you can self-host rmfakecloud on a local server or NAS. It officially supports the reMarkable 1 and acts as a drop-in replacement for the official sync service
.
3. Processing & Syncing to Obsidian Because the reMarkable uses a proprietary vector format (.rm) for notes, you will need a tool to convert them before they go into Obsidian
.
You can use community Python scripts like RemarkableSync or rmirro on your computer. These scripts connect to your tablet via SSH, pull the raw files, and convert them to PDFs or SVGs locally (preserving your templates and folder structures)
.
Alternatively, there is a powerful desktop app called RCU (reMarkable Connection Utility) that works offline over Wi-Fi or USB with the reMarkable 1. It can handle high-quality PDF rendering, extract your highlights as plain text, and take full disk snapshots to back up your device
.
4. Translating Handwriting The main hurdle remains the handwriting recognition (OCR). It still relies on the official cloud service (which uses a provider called MyScript)
. If you use rmfakecloud to host your own server, you can actually plug in your own MyScript API key to keep the handwriting-to-text conversion working without relying on reMarkable's servers
.
Bonus reMarkable 1 Hacks Since you have the rM1, you have access to some fun legacy hacks. If you end up buying a third-party stylus with a side-button (like the LAMY AL-Star EMR), there is a specific software hack confirmed to work on the reMarkable 1 that turns the pen's button into an automatic eraser
. Furthermore, if you ever want to completely abandon the note-taking interface, developers have actually ported a fully free GNU/Linux desktop OS called Parabola-rM exclusively to the reMarkable 1
.
In short, pull that reMarkable 1 out of the drawer—it is the ideal tinkerer's device for your S3/Obsidian project!