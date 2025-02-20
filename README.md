# S3-Glacier-Bulk-Folder-Restore
A Python script to **recursively** restore **all** objects in a given folder (prefix) stored in Amazon S3 Glacier or Deep Archive, using the **Bulk** retrieval tier. It scans subfolders under that prefix and initiates **temporary** restores—avoiding the need to manually restore each file in the AWS Console one by one.
