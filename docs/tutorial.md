## Android

- 在设置中打开开发者模式

- 在开发者选项中打开USB调试和允许ADB调试（如果有）

- 在电脑上下载[ADB调试工具](https://developer.android.com/tools/releases/platform-tools?hl=zh-cn)

- 用数据线连接电脑，然后再命令行执行

  ```bash
  adb devices -l
  ```

  预期显示你的设备，且设备标识符右侧显示`device`代表已经正常连接

- 右侧连接设备

## iOS

- 运行项目文件夹中的start.cmd，会自动安装iOS开发者选项工具

- 在命令行执行

  ```bash
  python -m pymobiledevice3 amfi reveal-developer-mode
  ```

- 在设置->隐私与安全性->开发者选项中启用开发者模式，并按照要求重新启动手机来启动开发者模式

- 再次运行start.cmd，并打开网页应用

- 在右侧连接设备

## HarmonyOS

- 在设置中打开开发者模式

- 在电脑上下载[HarmonyOS SDK](https://developer.huawei.com/consumer/cn/download/)

- 打开USB调试后，在命令行执行命令来检测连接情况

  ```bash
  hdc list targets
  ```

- 在右侧连接设备