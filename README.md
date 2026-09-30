# 个人 Gmail 自动回复

定时查看自己的未读信，按 `profile.json` 里的事实，用你的语气起草回复。默认只演练：回复写在本机，不发信。后台页面只绑定 `127.0.0.1`。

## 准备

1. 安装 Python 3.10 或更新版本，然后在这个目录执行：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

2. 在 `.env` 里填 `OPENAI_API_KEY`。要换模型就改 `OPENAI_MODEL`。密钥不要写进代码。

3. 在 [Google Cloud](https://console.cloud.google.com/) 里启用 Gmail API，创建 OAuth 客户端，类型选「桌面应用」。把下载的 JSON 放到本目录，命名为 `credentials.json`。同意屏幕如果还在测试状态，把你自己的 Gmail 加为测试用户。测试状态的授权大约 7 天后会过期，过期后需要再同意一次。

4. 打开 `profile.json`，把占位内容改成你自己的事实。也可以等程序启动后在页面里改。

## 运行

```powershell
python main.py
```

浏览器会在第一次授权时打开。同意之后，凭证留在本机的 `token.json`，以后再运行不会为了授权打开浏览器。终端会打印：

`http://127.0.0.1:8765`

只跑一轮、不打开页面：

```powershell
python main.py --once
```

## 页面上能做的事

- 看最近的收件箱，正文按文本显示。
- 区分已自动回复、演练已保存、已跳过、未处理。
- 按当前模式立刻跑一轮。
- 勾选确认后，开启真实发送并立刻跑一轮；再次点击会关掉真实发送并再跑一轮。
- 编辑个人资料。

程序开着的时候，大约每小时自动跑一轮。

## 它会怎么处理信

- 只处理收件箱里的未读信，跳过促销、社交、论坛和聊天。
- 回复用第一人称，先回应来信里的具体事情。英文来信整封英文，中文来信整封中文。
- 只用资料里写明的事实。没有的经历、电话、地址和承诺不会编。
- 不回复自己、系统地址、邮件列表、自动回复，以及涉及密码、验证码、证件、银行卡的信。这类信也不会交给模型。
- 同一封信只处理一次。生成失败时下一轮会再试一次，还是不行就停止。
- 演练时把回复写到 `data/outbox/`，不标已读。真实发送时回复留在原会话，并把原信标成已读。
- 个人资料仍是「示例用户」或带「请填写」这类占位文字时，不能开启真实发送。

不要把 `.env`、`token.json`、`credentials.json` 发给别人。`profile.json` 会变成你的个人资料，放进仓库前先看一遍。

## 测试

```powershell
python -m unittest discover -s tests -v
```
