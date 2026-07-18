---
name: Agent Memory Gateway Admin
description: 克制、可信、可行动的共享记忆管理工作台
colors:
  canvas: "oklch(0.99 0 0)"
  surface: "oklch(0.968 0.004 260)"
  surface-strong: "oklch(0.945 0.006 260)"
  ink: "oklch(0.22 0.018 260)"
  muted: "oklch(0.44 0.018 260)"
  line: "oklch(0.885 0.01 260)"
  accent: "oklch(0.52 0.16 264)"
  accent-soft: "oklch(0.94 0.025 264)"
  success: "oklch(0.48 0.13 150)"
  warning: "oklch(0.61 0.13 78)"
  danger: "oklch(0.52 0.16 28)"
typography:
  headline:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "normal"
rounded:
  control: "7px"
  surface: "10px"
  dialog: "12px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "20px"
  xl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "36px"
  panel:
    backgroundColor: "#ffffff"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "14px"
  badge:
    backgroundColor: "#ffffff"
    textColor: "{colors.muted}"
    rounded: "999px"
    padding: "0 8px"
    height: "22px"
---

# Design System: Agent Memory Gateway Admin

## 1. Overview

**Creative North Star: "安静的控制室"**

管理页是一间安静但信息完整的控制室。它不抢用户注意力，而是用清楚的层级、稳定的布局和准确的状态帮助用户迅速判断：系统是否健康、哪里需要处理、动作会影响什么。界面保持类苹果系统工具的克制感，但信息密度和操作能力应达到成熟运维产品的水平。

全局采用固定侧栏和自适应内容区。数据表、活动流和设备列表应充分使用可用宽度；说明文字保持 65–75 个字符的舒适行长。页面拒绝“大面积空白、固定窄栏和低信息密度造成的半成品后台”，也拒绝用装饰动画和卡片墙制造层级。

**Key Characteristics:**

- 冷静的浅色中性表面和单一蓝色强调色。
- 状态、来源和下一步始终同时出现。
- 数据区域铺满可用空间，说明文字单独限制行长。
- 动画只解释状态变化，持续 150–200ms，并支持减少动态效果。

## 2. Colors

颜色承担层级和状态，不承担装饰。蓝色只用于主操作、当前导航和焦点；绿色、琥珀色、红色只用于语义状态。

### Primary

- **控制蓝**：用于主按钮、当前导航、焦点环和关键可操作元素。
- **控制蓝浅层**：用于选中、悬停和低强度提示背景。

### Neutral

- **工作台白**：页面画布和主要内容背景。
- **冷灰表面**：侧栏、面板标题和分组区域。
- **石墨文字**：标题、正文与关键数据。
- **雾灰文字**：辅助说明、时间和技术标识。
- **细线灰**：分隔线、表格边界和静止控件边框。

### Named Rules

**The One Accent Rule.** 蓝色强调色只用于当前状态和主要操作，单屏面积不超过约 10%。

**The Semantic Color Rule.** 绿色、琥珀色、红色必须同时配合文字或图标，不能单独表达状态。

## 3. Typography

**Display Font:** 系统无衬线字体栈

**Body Font:** 系统无衬线字体栈

**Label/Mono Font:** `ui-monospace`, `SFMono-Regular`, `Consolas`

**Character:** 字体选择服从可读性和平台一致性。中文、英文、数字和技术标识保持清晰，不使用展示字体制造品牌感。

### Hierarchy

- **Headline**（700，28px，1.25）：页面标题，每页仅一个。
- **Title**（650，15px，1.4）：面板标题、设备和活动主信息。
- **Body**（400，14px，1.55）：说明和操作信息；连续说明限制在 65–75ch。
- **Label**（600，12px，1.4）：字段名、表头和辅助状态，不强制全大写。

### Named Rules

**The Plain Language Rule.** 主层级使用用户能理解的名称；内部 ID 和错误码只能出现在次级详情中。

## 4. Elevation

系统以色调分层和 1px 边界为主。静止面板不使用明显阴影；对话框和悬浮提示只使用轻微环境阴影，避免产生漂浮卡片墙。

### Shadow Vocabulary

- **环境低层**（`0 1px 2px oklch(0.22 0.018 260 / .04)`）：当前导航和轻微悬停反馈。
- **确认层**（`0 8px 12px oklch(0.2 0.02 260 / .12)`）：仅用于需要阻断页面操作的确认对话框。

### Named Rules

**The Flat-by-Default Rule.** 页面静止时保持平坦；只有焦点、悬停和确认层可以短暂获得高度。

## 5. Components

### Buttons

- **Shape:** 轻微圆角（7px），高度 36px；触控布局提高到至少 44px。
- **Primary:** 控制蓝底、白字，只用于当前流程的主要提交。
- **Hover / Focus:** 160ms 色彩过渡；键盘焦点使用 2px 控制蓝外环。
- **Secondary / Ghost:** 白色或透明背景配细线边框；危险操作保持白底红字，确认后才使用红底。

### Chips

- **Style:** 22px 高的紧凑状态标签，用浅色底、文字和边框共同表达语义。
- **State:** 状态标签不可伪装成按钮；可交互筛选器必须有清晰的悬停、焦点和选中状态。

### Cards / Containers

- **Corner Style:** 克制圆角（10px）。
- **Background:** 白色内容面配冷灰标题带。
- **Shadow Strategy:** 默认无阴影，以边界和色调分层。
- **Border:** 1px 细线灰。
- **Internal Padding:** 14–15px；数据表可使用 10–12px 单元格间距。

### Inputs / Fields

- **Style:** 白底、1px 边框、7–8px 圆角，高度 36px。
- **Focus:** 2px 控制蓝外环，不能只改变边框颜色。
- **Error / Disabled:** 错误同时提供文字；禁用状态保留可读性并说明原因。

### Navigation

桌面端使用固定侧栏，当前项以白色表面、控制蓝文字和状态点表示。窄屏转为顶部网格导航，点击目标至少 44px。页面切换保留位置语义，并把焦点移到页面标题。

### Data Tables

表格占满内容区。主信息使用自然语言，来源、状态、更新时间和操作保持可扫读；技术标识折叠显示。窄屏优先横向滚动或结构化堆叠，不通过缩小字号硬塞内容。

## 6. Do's and Don'ts

### Do:

- **Do** 让数据区域使用全部可用宽度，只给连续说明文字设置行长上限。
- **Do** 为待审核、异常、设备和权限状态提供紧邻的处理入口。
- **Do** 在活动记录中同时显示友好的设备名、Agent 名、类型、状态和技术引用。
- **Do** 对权限变更和撤销操作执行显式确认、并发版本校验和审计记录。
- **Do** 为加载、空状态、部分失败、无权限和会话失效提供明确恢复路径。

### Don't:

- **Don't** 做“只展示数据却不能处理问题的只读看板”。
- **Don't** 让用户“依赖反复复制命令或临时链接才能进入管理流程”。
- **Don't** 使用“大面积空白、固定窄栏和低信息密度造成的半成品后台”。
- **Don't** 使用“过度圆角、玻璃拟态、渐变堆叠、装饰性动画和模板化 SaaS 卡片墙”。
- **Don't** “用内部错误码、数据库字段或技术标识替代用户能理解的状态与说明”。
