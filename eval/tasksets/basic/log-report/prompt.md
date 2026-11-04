# Summarise an access log

`logs/access.log` is a web server log in the common format:

    IP - - [timestamp] "METHOD PATH HTTP/1.1" STATUS BYTES

Write `report.txt` in the workspace root with exactly these five lines (values computed from the log):

    total_requests <number of lines>
    error_requests <lines with STATUS >= 500>
    top_ip <ip> <count>          (most frequent client IP; ties -> lexicographically smallest IP)
    top_path <path> <count>      (most requested PATH; ties -> lexicographically smallest path)
    bytes_total <sum of BYTES>

Fields are separated by single spaces, no trailing spaces. You may put a helper script under `bin/`.
