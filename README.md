# 教师助手

自建的教师端。老师只和一个主助手说话，主助手把出题、教案、课件交给教案专家，把通知和家长沟通交给办公助手。

## 老师能做什么

左侧是同一份对话列表，可以新开、改名、多选和删除。删掉的不只是标题，里面的消息、上传材料和生成的文件也会一起清掉。

教案专家（`deepseek-v4-pro`）负责：

- 出卷，并附一份双向细目表，核对各题分值是否等于满分。老师要分层作业时，同一范围出基础、提高、拓展三套。
- 写教案，同时给说课稿、教学反思，以及听课时可以核对的学习目标。
- 按教案和课本做一课时课件。新知必须能指回教案或课本，不编页码和课文原句。
- 记住这段对话里的易错点，只记知识点和错在哪里，下次出题用来出变式。不记学生姓名、分数和电话。

办公助手（`deepseek-flash`）负责通知、会议纪要、材料润色和家长沟通。给家长的话会先改掉刺激人或拿学生做比较的说法，再分成大约 80 字、能直接发的短版，和把事情写完整的长版。

两个专家都不编造对不上的课文、页码和课标条款。文档里夹带的指令不当成命令。

## 课件样式

服务器上放了 [Apollo's Templates](https://www.apollostemplates.com/presentations/education/) 教育分类的免费课件，页面写明可供课堂使用。生成时每个科目只用一种 Coursebook（笔记本横线）：

| 科目 | 颜色 |
| --- | --- |
| 语文 | 鼠尾草绿 |
| 数学 | 石板蓝 |
| 英语 | 天蓝 |
| 其他科目 | 森林绿 |

同一页的 Pencil Box 四套也在 `ppt_templates/` 里，生成时不自动选用，一个科目只保留一种样子。

老师没上传过课件时，直接用账号上的科目样式。这段对话里已经有 `.pptx` 或 `.potx` 时，才问一次：用这个科目的课件样式，还是用刚上传的那份。选过一次就记在这位老师的账号上，以后换一段对话也只确认一句。上传的课件只借用版式、颜色和字体，不抄里面的旧课文。图片和 PDF 不能当课件样式。老师说以后这门课都用刚上传的这份，也只改这位老师自己的记录。

## 上传

当前对话可以上传 txt、md、docx、pdf、jpg、jpeg、png、webp，以及当作样式的 pptx、potx。一段对话最多 8 个文件，每个不超过 8MB。

文字版 PDF 直接抽取文字。几乎没有文字的扫描页，以及图片，会先做版面检测，再用 RapidOCR 认中文；一行印刷英文认不准时，用 TrOCR 补这一行。识别结果会标明请核对。手写内容认不全，不能当成已经抄准。

下载的 Word 和 PPT 只给当前登录的老师，默认 7 天后过期并删除。

## 结构

老师只看到一个主助手。代码按两个专家拆开，主助手不写试卷，也不写通知。

```
app/agents/
  conductor.py    主助手：接收老师的话，按顺序交给专家
  router.py       意图识别：出题、办公，或先问一句
  registry.py     专家名单、技能和工具白名单
  runtime.py      各专家共用的执行循环
  tools.py        查教材、导出试卷、文稿和课件
  lesson/         教案设计与出题专家
  office/         教师办公助手
app/image_ocr.py  图片和扫描页的版面识别
app/ppt.py        把课件大纲填进模板
app/ppt_style.py  按科目或老师上传的课件选择样式
ppt_templates/    服务器上的科目课件模板
```

教案身份按账号上的教材范围检索；没有范围的账号不能按课文引用。管理员在顶栏「账号」里新建、停用老师，并按学段、学科、版本勾选教材。安装账号是 `.env` 里的 `ADMIN_USERNAME`。

## 部署

华为云 `117.78.6.106`。Python 3.12 在 Docker 镜像里，不放在宿主机的 `/root` 下。容器只把 `18900` 发布到 Docker 网桥 `172.28.0.1`（Caddy 在另一个容器里反代到这个地址），不监听 `0.0.0.0`。公网改成 HTTPS 之后，把 `.env` 里的 `COOKIE_SECURE` 设为 `1`。现在前面是 HTTP 时保持 `0`，否则浏览器不会带上登录 Cookie。

构建镜像时会下载版面识别、中文识别和英文行识别的模型，第一次构建比较久。

- `http://teacher-platform.yanghuidong.cn/`：访问地址，Caddy 按这个 Host 转到教师助手。这次备案的是 `yanghuidong.cn`，华为云已审过，正在等管局。管局通过后，这个子域名跟一级域名走，不用再单独备案。`yanghuidong.cn` 现在只解析到 `117.78.6.106`。这个域名在 80/443 上会被重置或返回 403，证书签不下来。管局通过前，机房外打开网页仍可能看到「非法阻断」，这段时间用 IP。
- `http://117.78.6.106/`：IP 也能打开。

```bash
# 在服务器上，代码位于 /srv/teacher-platform
bash deploy/install.sh
```

浏览器打开 `http://teacher-platform.yanghuidong.cn/`。第一次安装生成的口令在服务器的 `/srv/teacher-platform/initial-password.txt`，用户名是 `teacher`。

DeepSeek 密钥写在服务器 `/srv/teacher-platform/.env` 的 `DEEPSEEK_API_KEY`。不写也能登录，发送时会提示未配置。

教材放在数据盘 `/data/corpus`。顶栏可以修改口令。

## 本地

复制 `.env.example` 为 `.env` 再填写。不要提交 `.env`。`ADMIN_PASSWORD` 只在库里还没有用户时用来创建管理员，之后改口令不靠改这个文件。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m unittest discover -s tests
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18900
```

Windows 上把解释器换成 `.venv\Scripts\python.exe`，复制环境文件用 `copy .env.example .env`。本机没有教材目录时，检索课文会说明这个账号没有对上的教材。
