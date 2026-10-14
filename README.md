# 教师助手

自建的教师端。两个身份在顶栏手动切换：教师办公助手、教案设计与出题专家。不依赖 OpenClaw。

现网 `openclaw-teacher.xiaoyangtx.cloud` 仍在腾讯云，本项目不替换它。教材和教师账号会拷到华为云，腾讯云上的原件保留。

## 部署

华为云 `117.78.6.106`。Python 3.12 在 Docker 镜像里，不放在宿主机的 `/root` 下。容器只把 `18900` 发布到 Docker 网桥 `172.28.0.1`（Caddy 在另一个容器里反代到这个地址），不监听 `0.0.0.0`。`test.xiaoyangtx.cloud`、`www.xiaoyangtx.cloud` 仍走面试站点。公网改成 HTTPS 之后，把 `.env` 里的 `COOKIE_SECURE` 设为 `1`。

- `http://teacher-platform.xiaoyangtx.cloud/`：访问地址。解析已经指向 `117.78.6.106`，Caddy 按这个 Host 转到教师助手。现有证书不含这个子域名，所以先走 HTTP。这个域名还没在华为云备案接入，机房外访问会看到「非法阻断」，备案完成前外网仍用 IP。
- `http://117.78.6.106/`：IP 也能打开。
- `http://teacher-platform.yanghuidong.cn/`：服务器上已经接到本进程。公网解析里还有一条内网地址 `192.168.9.58`，要删掉，只留 `117.78.6.106`。另外，这个域名在 80/443 上会被重置或返回 403，证书签不下来。

```bash
# 在服务器上，代码位于 /srv/teacher-platform
bash deploy/install.sh
```

浏览器打开 `http://teacher-platform.xiaoyangtx.cloud/`。第一次安装生成的口令在服务器的 `/srv/teacher-platform/initial-password.txt`，用户名是 `teacher`。

DeepSeek 密钥写在服务器 `/srv/teacher-platform/.env` 的 `DEEPSEEK_API_KEY`。不写也能登录和切换身份，发送时会提示未配置。

教材在数据盘 `/data/corpus`（`/srv/corpus` 是指过去的链接），从腾讯云原样拷来，腾讯云上的那份还留着。教师账号从原来的花名册导入，口令哈希不变，所以原来的用户名和口令可以登录。教案身份按账号上的教材范围检索；没有范围的账号不能按课文引用。助手回复可以下载成 Word，文件只给当前登录的老师。顶栏可以修改口令。

每个身份可以新开对话，左侧能翻回以前的对话。当前对话可以上传 txt、md、docx、pdf，助手只把里面的文字当资料。安装账号（`.env` 里的 `ADMIN_USERNAME`）是管理员，顶栏有「账号」：可以新建、停用，并按学段学科版本勾选教材。

## 本地

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m unittest tests/test_auth_and_switch.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18900
```
