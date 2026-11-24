// 朴素、规范的中文技术文档模板（Typst）
// 用法：
//   #import "../template/cpc-doc.typ": *
//   #show: doc.with(title: "...", subtitle: "...", version: "v0.1", date: "2026-11-24", ...)

#let serif = ("Times New Roman", "Noto Serif CJK SC", "Source Han Serif SC", "SimSun", "Songti SC")
#let sans = ("Arial", "Noto Sans CJK SC", "Source Han Sans SC", "SimHei", "Microsoft YaHei", "PingFang SC")
#let mono = ("Consolas", "DejaVu Sans Mono", "Menlo", "Noto Sans Mono CJK SC", "SimHei", "Microsoft YaHei")

#let ink = rgb("#0b0b0b")
#let ink2 = rgb("#52514e")
#let rule-color = rgb("#c3c2b7")
#let code-bg = rgb("#f6f6f4")
#let accent = rgb("#2a78d6")

// ---------------------------------------------------------------- 组件

// 三线表：表头上下各一条线，表尾一条线
#let tbl(columns: auto, align: left, header: (), ..cells) = {
  let n = if type(columns) == int { columns } else { columns.len() }
  table(
    columns: columns,
    align: align,
    stroke: none,
    inset: (x: 6pt, y: 4.5pt),
    table.hline(stroke: 1pt),
    table.header(..header.map(h => text(font: sans, weight: "bold", size: 9pt, h))),
    table.hline(stroke: 0.5pt),
    ..cells.pos().map(c => text(size: 9pt, c)),
    table.hline(stroke: 1pt),
  )
}

// 提示框
#let note(title: "说明", body) = block(
  width: 100%, inset: (x: 10pt, y: 8pt), radius: 2pt,
  fill: rgb("#eef4fc"), stroke: (left: 2.5pt + accent),
  { set par(justify: false, first-line-indent: 0em); [#text(font: sans, weight: "bold", size: 9.5pt, fill: accent)[#title] #h(0.5em) #text(size: 9.5pt, body)] },
)
#let warn(title: "注意", body) = block(
  width: 100%, inset: (x: 10pt, y: 8pt), radius: 2pt,
  fill: rgb("#fdf3ec"), stroke: (left: 2.5pt + rgb("#eb6834")),
  { set par(justify: false, first-line-indent: 0em); [#text(font: sans, weight: "bold", size: 9.5pt, fill: rgb("#b4481d"))[#title] #h(0.5em) #text(size: 9.5pt, body)] },
)

// 键值信息表（封面等处）
#let kv(..pairs) = grid(
  columns: (5em, auto), row-gutter: 0.7em, column-gutter: 1em,
  ..pairs.pos().map(((k, v)) => (text(font: sans, fill: ink2, k), v)).flatten(),
)

// ---------------------------------------------------------------- 文档

#let doc(
  title: "", cover-title: none, subtitle: "", doc-id: "", version: "v0.1", date: "", org: "",
  authors: (), revisions: (), abstract: none, body,
) = {
  set document(title: title)
  set page(
    paper: "a4",
    margin: (top: 2.6cm, bottom: 2.4cm, left: 2.5cm, right: 2.5cm),
  )
  set text(font: serif, size: 10.5pt, lang: "zh", region: "cn", fill: ink)
  set par(justify: true, leading: 0.85em, spacing: 1.05em, first-line-indent: (amount: 2em, all: true))
  show strong: set text(font: sans, weight: "bold")
  show emph: set text(font: ("Times New Roman", "KaiTi", "STKaiti", "Noto Serif CJK SC"))

  // 标题
  set heading(numbering: "1.1.1")
  show heading: set text(font: sans, weight: "bold")
  show heading.where(level: 1): it => {
    pagebreak(weak: true)
    v(0.6em)
    block(text(size: 16pt, it), below: 1.1em)
  }
  show heading.where(level: 2): it => block(text(size: 13pt, it), above: 1.4em, below: 0.9em)
  show heading.where(level: 3): it => block(text(size: 11pt, it), above: 1.2em, below: 0.8em)
  show heading.where(level: 4): it => block(text(size: 10.5pt, it), above: 1em, below: 0.7em)

  // 代码
  show raw: set text(font: mono, size: 9pt)
  show raw.where(block: false): it => box(fill: code-bg, inset: (x: 2.5pt, y: 0pt), outset: (y: 2.5pt), radius: 1.5pt, it)
  show raw.where(block: true): it => block(
    width: 100%, fill: code-bg, inset: (x: 8pt, y: 7pt), radius: 2pt, stroke: 0.5pt + rgb("#e1e0d9"),
    { set par(justify: false, first-line-indent: 0em); set text(size: 7.8pt); it },
  )

  // 图表与列表
  set figure(gap: 0.8em)
  show figure.where(kind: table): set figure.caption(position: top)
  show figure.caption: set text(font: sans, size: 9pt, fill: ink2)
  set figure(supplement: it => if it.func() == table { "表" } else { "图" })
  set list(indent: 1em, body-indent: 0.5em)
  set enum(indent: 1em, body-indent: 0.5em)
  show list: set par(first-line-indent: 0em, justify: false)
  show enum: set par(first-line-indent: 0em, justify: false)
  show table: set par(justify: false, first-line-indent: 0em)
  show link: it => text(fill: accent, it)
  show terms: set par(first-line-indent: 0em)
  // 交叉引用：章 → "第 N 章"，节 → "第 N.M 节"
  show ref: it => {
    let el = it.element
    if el != none and el.func() == heading {
      let n = numbering("1.1", ..counter(heading).at(el.location()))
      link(el.location(), if el.level == 1 [第 #n 章] else [第 #n 节])
    } else { it }
  }

  // ---------- 封面
  page(header: none, footer: none, margin: (x: 3cm, top: 5cm, bottom: 3cm), {
    set par(first-line-indent: 0em)
    text(font: sans, size: 11pt, fill: ink2, org)
    v(1.2cm)
    line(length: 100%, stroke: 1.2pt + ink)
    v(0.6cm)
    text(font: sans, size: 22pt, weight: "bold", if cover-title != none { cover-title } else { title })
    v(0.3cm)
    text(font: sans, size: 15pt, fill: ink2, subtitle)
    v(0.5cm)
    line(length: 100%, stroke: 0.5pt + rule-color)
    v(1.5cm)
    kv(
      ("文档编号", doc-id),
      ("版本", version),
      ("日期", date),
      ("编写", authors.join("、")),
    )
    if abstract != none {
      v(1.5cm)
      block(width: 100%, inset: (left: 0pt), {
        text(font: sans, weight: "bold", size: 10pt)[摘要]
        v(0.3em)
        set text(size: 10pt, fill: ink2)
        set par(first-line-indent: (amount: 2em, all: true))
        abstract
      })
    }
  })

  // ---------- 页眉页脚（从修订记录页开始）
  set page(
    header: context {
      set text(font: sans, size: 8.5pt, fill: ink2)
      grid(columns: (1fr, auto), title + " · " + subtitle, version)
      v(-0.5em)
      line(length: 100%, stroke: 0.5pt + rule-color)
    },
    footer: context {
      set text(font: sans, size: 8.5pt, fill: ink2)
      align(center)[第 #counter(page).display() 页 / 共 #counter(page).final().first() 页]
    },
  )
  counter(page).update(1)

  // ---------- 修订记录与目录
  if revisions.len() > 0 {
    block(text(font: sans, size: 14pt, weight: "bold")[修订记录], below: 1em)
    tbl(columns: (5em, 7em, 6em, 1fr), header: ("版本", "日期", "作者", "说明"), ..revisions.flatten())
    v(1.5em)
  }
  {
    show outline.entry.where(level: 1): it => { v(0.6em, weak: true); strong(it) }
    outline(title: text(font: sans, size: 14pt)[目录], indent: auto, depth: 3)
  }
  body
}
