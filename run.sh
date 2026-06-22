python parse_wit.py WIT12.TXT -o out.txt
python parse_wit.py WIT12.TXT -o out.txt --no-quirk   # 不复刻错位 bug，AccX 与其它字段严格同包对齐
python parse_wit.py WIT12.TXT -o out.csv              # 输出后缀为 .csv 时自动导出标准CSV（逗号分隔, utf-8-sig编码）
python parse_wit.py WIT12.TXT -o out.txt --format csv # 也可用 --format 强制指定格式，与输出文件后缀无关
python parse_wit.py WIT12.TXT -o labelstudio.csv      # 文件名含 "labelstudio" 自动导出 Label Studio 格式
python parse_wit.py WIT16.TXT -o WIT16.csv --format labelstudio  # 也可用 --format 强制指定
