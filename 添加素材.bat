@echo off
rem 傻瓜式素材安装: 把豆包/即梦生成的视频或音频文件拖到本文件图标上
set PAUSE_ONLY=1
python "%~dp0scripts\add_asset.py" %*
pause
