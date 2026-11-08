---
name: zhineng-sticker
description: 用 GPT Image 2.6 批量做微信动态表情包：选题库一键起套、角色图锁定形象、4×4 精灵图切成 240×240 GIF、按微信规范检查、出投稿素材包和上架文案，附赞赏 / 付费 / 定制接单的做法。用于「做一套表情包」「批量做表情包」「把我家猫做成表情包」「接了个表情包定制单」「上架微信表情」。不代替用户注册和提交，不做他人 IP。
---

# 智能杠杆 · 表情包工厂

一个角色进，一套（或好几套）能上架、能交付的动态表情包出。默认一套 8 张 GIF 加 5 张配套素材。

脚本：`scripts/sticker.py`（Python 3.9+，`pip install pillow`）。Windows 上用 `python`，macOS / Linux 用 `python3`。下面统一写 `python`。

## 0. 先确定用什么出图

按顺序问自己，用第一个能用的：

1. **你就在 Codex 里运行**（ChatGPT 账号登录的 Codex，不用 API key）：用你**内置的图片生成工具**出图，每张图都把角色图作为参考图附上，生成后把文件存到第 4 步说的位置。
2. **有 `OPENAI_API_KEY`**（中转站再设 `OPENAI_BASE_URL`）：用脚本的 `gen` 命令自动出图。
3. **都没有**：用 `prompts` 命令把提示词写成文件，让用户复制到 ChatGPT 网页（免费版也能用 GPT Image 2.6）出图，下载后放回目录。

## 1. 先问清权利，这一步不能跳

素材必须是下面一种，不是就停下来说明原因：

- 用户自己的原创角色、自家宠物；
- 用户本人，或已取得本人同意的成年人，要做成明显的卡通形象；
- 客户下单提供的素材，客户能确认有权使用。

不做：明星、影视动漫游戏角色、网络二创形象、别人的画作。微信审核不收未授权的二创和同人；未成年人肖像不能投稿，只能私下给家长自用。

## 2. 起一套

```bash
python scripts/sticker.py new projects/打工猫 --theme 打工人 --count 8 --name 打工橘喵
```

选题库有 `打工人` / `萌宠` / `情侣` / `长辈祝福` 四个主题，每个 16 句，`--count 8` 或 `16`。生成的 `pack.json` 里改名称、介绍、版权、赞赏引导语，配字和动作按用户需要增删改。写之前读 [选题和文案](references/writing.md)。

**批量**：同一个角色换主题，或者同一主题换角色，就是多建几个目录，后面的命令都能一次给多个目录。

把 8 句话给用户过一遍，定了再出图。

## 3. 定角色

- 用户已有干净的角色图：存成项目目录下的 `character.png`。
- 只有照片：`python scripts/sticker.py character --photo 照片.jpg --out projects/打工猫/character.png`（要 API key）；在 Codex 里就用内置出图，提示词照 `cmd_character` 里那段写。
- 什么都没有、要原创角色：按 [风格菜单](references/styles.md) 挑一种风格，写一段角色描述出一张全身正面、透明底、无文字的角色图。

出完给用户看一眼，确认了再往下。这张图是整套形象一致的锚，不满意就重出。

## 4. 出图

先跑：

```bash
python scripts/sticker.py prompts projects/打工猫
```

它把每张缺的图的提示词写进 `prompts/`，并列出要存到哪：

| 存到 | 用哪个提示词 | 说明 |
|---|---|---|
| `sheets/01.png` … | `prompts/sheet_01.txt` … | 4×4 动画精灵图，正方形，透明底 |
| `assets_raw/cover.png` 等 5 张 | `prompts/cover.txt` 等 | 封面、图标、横幅、赞赏引导图、致谢图 |

- **Codex 内置出图**：逐个读提示词文件，附上 `character.png` 出图，存到对应位置。一张一张来，每张出完先看一眼：是不是 4×4、16 格动作连贯、配字有没有错字、角色有没有走样，不行就重出这一张。
- **API key**：`python scripts/sticker.py gen projects/打工猫 --dry-run` 先报价，用户同意后去掉 `--dry-run`；想重抽某几张加 `--redo 3,7`。
- **ChatGPT 网页**：把提示词和角色图发给用户，让用户出图后下载，按表里的文件名放好。

## 5. 合成和检查

```bash
python scripts/sticker.py all projects/打工猫            # 合成 + 检查
python scripts/sticker.py all projects/打工猫 projects/萌宠猫   # 批量
```

- `build` 会自动找格子之间的空隙来切（模型画的格子常常不齐）；图是白底的会自动抠成透明，并提示看一眼白描边。
- `check` 的 FAIL 必须清零。WARN 要人看。
- 然后**亲眼看**：打开 `预览.html`，逐张看配字、角色、循环有没有跳帧，切深色背景看有没有白边。不满意就重出那张精灵图。循环跳一下又不想重出：在 pack.json 那一张加 `"keep_frames": [1, 13]`（只留第 1–13 帧）再 `build`。不要直接改 GIF，下次 `build` 会被覆盖。

## 6. 交付

| 文件 | 用途 |
|---|---|
| `submit/` | 投稿素材，按微信字段命名 |
| `上架文案.md` | 名称、介绍、含义词，照填 |
| `预览.html`、`预览.png` | 给自己或客户确认 |
| `交付给客户.zip` | 定制单直接发给客户 |

上架步骤见 [上架微信](references/wechat.md)，三种赚钱方式见 [变现](references/money.md)，接定制单见 [定制接单](references/custom-order.md)。注册和提交要用户本人实名操作，不要代做。

## 边界

- 不承诺收入和过审。过审由微信决定。
- 投稿时表单里有「AI 生成」相关的声明，提醒用户如实勾选。
- 定制单交付前提醒客户：表情归客户自用；要投稿到微信，权利和实名是投稿人自己的责任。
