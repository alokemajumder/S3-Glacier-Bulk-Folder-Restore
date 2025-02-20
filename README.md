# S3 Glacier Bulk Folder Restore (Temporary Access)

A Python script to **recursively** restore **all** objects stored in Amazon S3 Glacier or Deep Archive under a given prefix (folder). This is particularly helpful when you have **large numbers** of Glacier or Deep Archive objects that would otherwise require individual manual restores in the AWS Console.

## Problem Statement

By default, the AWS Console forces you to **restore each file individually**, which is impractical if you have thousands (or millions) of archived objects. This script solves that problem by **automating** the restore process:

1. **Recursively** scans all subfolders under a specified prefix.
2. **Forces** the restore tier to **Bulk** to minimize costs (though it can be slower than Standard or Expedited).
3. Creates a **temporary** copy in S3 Standard for a chosen number of days (`restore_days`), after which the copy expires and you cease paying S3 Standard rates.

---

## Disclaimer

- **Use at your own risk.**  
- Always review your AWS costs and your account limits before running large-scale bulk restores.  
- This code is provided as-is without any warranties.  

---

## Features

- **Recursive** restore: Lists and restores **all** objects in Glacier/Deep Archive under a given prefix.  
- **User Prompts**:  
  - Checks bucket accessibility,  
  - Shows a sample of objects in that prefix,  
  - Asks for final confirmation before initiating the restore.  
- **Temporary Restore**: By default, the restored copy remains in S3 Standard for 30 days (configurable).  
- **Default Credential Chain**: If no credentials are provided, it falls back to environment variables, AWS CLI, or instance roles.

---

## Prerequisites

- **Python 3.7+**  
- **[Boto3](https://pypi.org/project/boto3/)** installed:
  ```bash
  pip install boto3

-   AWS Credentials with permissions:
    -   `s3:ListBucket` (to list objects under a prefix),
    -   `s3:RestoreObject` (to initiate restore),
    -   (Optionally) `s3:GetObject` if you plan to download them after restore.

----------

## Installation & Setup

1.  **Clone** or download this repository:
    
    `git clone https://github.com/alokemajumder/S3-Glacier-Bulk-Folder-Restore.git
    cd S3-Glacier-Bulk-Folder-Restore` 
    
2.  (Optional) **Create a virtual environment**:
    
    `python -m venv venv
    source venv/bin/activate
    # On Windows: venv\Scripts\activate` 
    
3.  **Install dependencies**:
   
    
    `pip install boto3` 
    

----------

## Usage

1.  **Run** the script:
    
    `python glacier_restore.py` 
    
2.  **Follow** the prompts:
    
    -   **Bucket Name**: e.g. `my-archive-bucket`
    -   **Prefix**: e.g. `folder-to-restore/`  
        The script **recursively** handles all objects under this prefix (including subfolders).
    -   **Number of days** to keep the restored copy (default is 30).
    -   **AWS Credentials** (optional). If you leave them blank, it uses your default AWS credential chain.
3.  **Confirm** when prompted. The script displays:
    
    -   A sample of objects under the prefix (up to 5 keys).
    -   Total objects found so far under that prefix.
    -   Bulk restore tier, to save costs.
4.  After you confirm (`y`), the script:
    
    -   Initiates **Bulk** restore requests for **all** Glacier/Deep Archive objects under the prefix.
    -   Skips objects that are already restored or not in Glacier classes.
    -   Displays a final summary.
5.  **Wait** for the restore to complete. Bulk restore can take hours, depending on the size and number of objects.
    

> **Note**: The **temporary** copy in S3 Standard will expire after `restore_days`. If you need the data permanently in S3 Standard, you must copy it to another bucket/key or set a lifecycle rule to transition the storage class.

----------

## Example


    `=== S3 Glacier TEMPORARY Bulk Restore Script ===
    Enter S3 bucket name: my-bucket
    Enter folder prefix (e.g., 'my-folder/'): backups/
    Enter the number of days to keep the restored copy (default 30): 7
    
    If you leave the following blank, the default AWS credential chain will be used.
    Enter AWS Access Key ID (or leave blank):
    Enter AWS Secret Access Key (or leave blank):
    
    [OK] Bucket 'my-bucket' is accessible.
    
    Checking objects under the prefix...
    
    Found 8 object(s) under prefix 'backups/'. Showing up to 5 sample keys:
      1. backups/2021/server1-data.gz
      2. backups/2021/server2-data.gz
      3. backups/2022/server1-archive.log
      4. backups/2022/server2-archive.log
      5. backups/2023/server3-archive.log
    
    --- Configuration ---
    Bucket: my-bucket
    Prefix: backups/
    Retrieval Tier: Bulk (Forced to minimize cost)
    Restore Days (TEMPORARY): 7
    
    Do you want to proceed with the TEMPORARY Bulk restore? (y/N): y
    
    Starting temporary Bulk restore process...
    
    [INITIATED] Bulk restore request started for: backups/2021/server1-data.gz
    [INITIATED] Bulk restore request started for: backups/2021/server2-data.gz
    ...
    
    --- Restore Summary ---
    Total initiated restores: 8
    Already in progress/restored: 0
    Skipped (not Glacier/Deep Archive): 0
    Errors: 0
    
    Restore process completed.` 

----------

## Contributing

Please see our [CONTRIBUTING.md](CONTRIBUTING.md) for details on how to open issues, suggest changes, or submit pull requests.

----------

## License

This project is open-sourced under the [MIT License](LICENSE). You’re free to use, modify, and distribute it as described in the license.
