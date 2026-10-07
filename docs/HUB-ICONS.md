# Hub icon refresh

The Hub checks selected app icons during its normal five-second app snapshot poll. It notices a new icon, a replacement, removal, or a higher-priority candidate without restarting the Hub or rescanning app manifests. App-local names keep priority; a matching file in a configured shared `Icons` folder is the fallback.

The browser cache key uses the selected path plus file size and modification time. Stable files keep the same URL and stay cached; a changed selection gets a new URL. Other Hub views that use `/api/apps/{id}/icon` are reconciled by the same poll. This refreshes the selected file; it does not copy a shared master icon into an app folder.

The revision is inexpensive file metadata, not a content hash. If an editing tool deliberately preserves both size and modification time when changing bytes, it must also update the modification time for automatic detection; rescanning alone does not change this cache key.
