import React, { useEffect, useMemo, useState } from "react";

type CatProfile = {
  age: string;
  historyIssues: string[];
  recentSymptoms: string[];
};

type ProfilePoint = {
  dimension: string;
  score: number | null;
  level?: string;
  type?: "pressure" | "protective" | "mixed";
  summary?: string;
};

type ProfileDiff = {
  dimension: string;
  product_a_score: number | null;
  product_b_score: number | null;
  diff_b_minus_a: number | null;
  type: "pressure" | "protective" | "mixed";
  a_level: string;
  b_level: string;
  summary: string;
};

type FriendlyRow = {
  dimension: string;
  current: string;
  target: string;
  interpretation: string;
  current_score: number | null;
  target_score: number | null;
};

type ProductInfo = {
  query: string;
  name: string;
  brand_name: string;
  ingredient_composition: string;
  profile: ProfilePoint[];
  baseline_profile: ProfilePoint[];
};

type ProductOption = {
  id: string;
  catalog_key: string;
  product_key?: string | null;
  label: string;
  brand: string;
  raw_brand?: string | null;
  product_name: string;
  raw_title?: string | null;
  origin_type?: string | null;
  brand_tier?: string | null;
  source: "score_db" | "taobao" | "merged" | string;
  source_item_id?: string | null;
  source_url?: string | null;
  price?: number | null;
  price_bucket?: string | null;
  food_taste?: string | null;
  net_content?: string | null;
  sold_text?: string | null;
  main_image_url?: string | null;
  main_images?: string[];
  compare_available: boolean;
  display_text?: string | null;
  function_tags?: Array<{ tag: string; display_tag?: string; level?: string; score?: number }>;
  warning_tags?: Array<{ tag: string; level?: string }>;
  quality_flags?: string[];
};

type CompareResult = {
  current_food: ProductInfo;
  target_food: ProductInfo;
  profile_diff: ProfileDiff[];
  friendly_rows: FriendlyRow[];
  core_diff_explanations: string[];
  tag_diff_summary: unknown;
  llm_context: unknown;
};

type UploadedImage = {
  fileName: string;
  previewUrl: string;
  file: File;
};

type MissingProductSubmission = UploadedImage & {
  brandName: string;
  productName: string;
  imageId?: string;
  orchestratorTaskId?: string;
};

type CatFoodTask = {
  id: string;
  task_type: string;
  status: "pending" | "running" | "success" | "failed";
  progress: number;
  error_message?: string;
  result?: {
    image_parse?: unknown;
    compare?: CompareResult;
    summary?: {
      summary: string;
      cached: boolean;
      cache_key: string;
    };
  };
  profile?: {
    current_food?: string;
    target_food?: string;
    cat_profile?: CatProfile;
  };
  images: Array<{
    id: string;
    product_name?: string;
    original_filename: string;
    parse_status: "pending" | "running" | "success" | "failed";
    parse_result?: {
      message?: string;
      file_name?: string;
      product_name?: string;
      sha256?: string;
    };
  }>;
};

type OrchestratorTask = {
  id: string;
  task_type: string;
  task_status: string;
  error_message?: string | null;
  nodes: Array<{
    node_code: string;
    node_name: string;
    node_status: string;
    priority: number;
    error_message?: string | null;
  }>;
};

const CAT_AGE_OPTIONS = ["0～1年", "1年", "2年～3年", "3～6年", "6年以上"];

const HISTORY_ISSUE_OPTIONS = [
  "无明显历史问题",
  "黑下巴",
  "软便/拉稀",
  "呕吐",
  "泪痕",
  "皮肤敏感",
  "泌尿问题",
  "挑食",
  "肥胖/体重管理",
  "疑似食物不耐受",
];

const RECENT_SYMPTOM_OPTIONS = [
  "无明显异常",
  "下巴出油",
  "粉刺/黑下巴",
  "软便",
  "拉稀",
  "呕吐",
  "食欲下降",
  "挑食",
  "泪痕加重",
  "便秘",
];

const DIMENSION_INTERPRETATION_CONFIG: Record<
  string,
  {
    displayName: string;
    type: "pressure" | "support" | "mixed";
    highStructure?: string;
    positiveMeaning?: string;
    supportAbility?: string;
    relatedView: string;
    symptomImpact: string;
  }
> = {
  蛋白质量: {
    displayName: "蛋白质量支持",
    type: "support",
    supportAbility: "动物蛋白质量、来源清晰度和蛋白正向支持",
    relatedView: "蛋白压力、软便标签和猫咪消化耐受",
    symptomImpact: "蛋白质量支持更低时，肠胃敏感猫可能更需要观察软便、便臭、食欲波动和换粮适应情况。",
  },
  蛋白压力: {
    displayName: "蛋白消化压力",
    type: "pressure",
    highStructure: "蛋白来源复杂、适应压力更高的结构",
    positiveMeaning: "蛋白结构更轻、换粮适应压力更低的方向",
    relatedView: "软便风险、呕吐表现和历史食物不耐受",
    symptomImpact: "蛋白消化压力更高时，食物不耐受、软便、拉稀、呕吐或换粮后食欲下降的猫需要重点观察。",
  },
  碳水负担: {
    displayName: "碳水负担",
    type: "pressure",
    highStructure: "淀粉、豆类或薯类负担更突出的结构",
    positiveMeaning: "碳水压力更轻、对软便和体重管理更友好的方向",
    relatedView: "肠胃友好度、软便标签和体重管理目标",
    symptomImpact: "碳水负担更高时，容易和软便、便便黏腻、体重管理压力、饭后腹胀感这类观察点相关。",
  },
  脂肪负担: {
    displayName: "脂肪负担",
    type: "pressure",
    highStructure: "油脂压力更突出的结构",
    positiveMeaning: "油脂压力更轻、对下巴和脂肪消化观察更友好的方向",
    relatedView: "黑下巴友好度、下巴出油和呕吐表现",
    symptomImpact: "脂肪负担更高时，下巴出油、粉刺/黑下巴、脂肪消化不适、呕吐或便便偏油需要重点观察。",
  },
  纤维缓冲: {
    displayName: "纤维缓冲支持",
    type: "support",
    supportAbility: "便便成形、粪便骨架和肠道缓冲支持",
    relatedView: "软便风险、便便状态和肠胃稳定性",
    symptomImpact: "纤维缓冲更低时，软便、拉稀、便便不成形、便秘或换粮期肠胃波动可能更明显。",
  },
  菌群支持: {
    displayName: "菌群支持",
    type: "support",
    supportAbility: "肠道菌群底物、短链脂肪酸和肠道稳定支持",
    relatedView: "软便风险、便便状态和换粮过渡期反应",
    symptomImpact: "菌群支持更低时，换粮期软便、便臭、便便状态不稳定和肠胃恢复速度需要重点观察。",
  },
  皮肤保护: {
    displayName: "皮肤保护支持",
    type: "support",
    supportAbility: "脂肪调节、抗氧化和皮肤稳定支持",
    relatedView: "黑下巴友好度、下巴出油和泪痕表现",
    symptomImpact: "皮肤保护支持更低时，下巴出油、黑下巴、皮肤敏感、泪痕或毛发状态需要结合日常表现观察。",
  },
};

function normalizeMultiSelect(options: string[], selected: string[], option: string) {
  if (selected.includes(option)) {
    const next = selected.filter((item) => item !== option);
    return next.length ? next : [options[0]];
  }

  if (option.includes("无明显")) {
    return [option];
  }

  return [...selected.filter((item) => !item.includes("无明显")), option];
}

function formatScore(score: number | null | undefined) {
  if (score === null || score === undefined || Number.isNaN(score)) return "暂无";
  return Number(score).toFixed(1);
}

function getDiffLevel(absDelta: number) {
  if (absDelta > 30) return "重点变化";
  if (absDelta >= 15) return "明显变化";
  return "非关键变化";
}

function diffTip(row: ProfileDiff, currentProductName: string, targetProductName: string) {
  if (row.diff_b_minus_a === null || row.diff_b_minus_a === undefined) {
    return "两款粮暂无足够数据进行比较。";
  }

  const delta = row.diff_b_minus_a;
  const config = DIMENSION_INTERPRETATION_CONFIG[row.dimension] || {
    displayName: row.dimension,
    type: row.type === "pressure" ? "pressure" : row.type === "protective" ? "support" : "mixed",
    highStructure: "该维度数值更高的结构",
    positiveMeaning: "该维度数值更低的方向",
    supportAbility: "该维度支持能力",
    relatedView: "风险标签和猫咪需求",
    symptomImpact: "这项变化需要结合猫咪近期症状、历史问题和实际换粮反应一起观察。",
  };
  const higherProduct = delta > 0 ? targetProductName : currentProductName;
  const lowerProduct = delta > 0 ? currentProductName : targetProductName;
  const absDelta = Math.abs(delta).toFixed(1);

  if (config.type === "pressure") {
    return `${higherProduct} 的${config.displayName}比 ${lowerProduct} 高 ${absDelta} 分，整体更偏${config.highStructure}；${config.symptomImpact}`;
  }

  if (config.type === "support") {
    return `${higherProduct} 的${config.displayName}比 ${lowerProduct} 高 ${absDelta} 分，${config.supportAbility}相对更突出；${config.symptomImpact}`;
  }

  return `${higherProduct} 在${config.displayName}上比 ${lowerProduct} 高 ${absDelta} 分；${config.symptomImpact}`;
}

function getRowDiffLevel(row: ProfileDiff) {
  if (row.diff_b_minus_a === null || row.diff_b_minus_a === undefined) return "暂无数据";
  return getDiffLevel(Math.abs(row.diff_b_minus_a));
}

function levelTone(value: string) {
  if (value.includes("高") || value.includes("强") || value.includes("优于")) return "bg-emerald-50 text-emerald-700";
  if (value.includes("低") || value.includes("弱") || value.includes("靠后")) return "bg-rose-50 text-rose-700";
  return "bg-slate-100 text-slate-700";
}

function optionTone(value?: string | null) {
  const text = value || "";
  if (text.includes("国产")) return "bg-emerald-50 text-emerald-700";
  if (text.includes("进口")) return "bg-blue-50 text-blue-700";
  if (text.includes("80")) return "bg-orange-50 text-orange-700";
  if (text.includes("慎选") || text.includes("未匹配") || text.includes("暂无评分")) return "bg-rose-50 text-rose-700";
  return "bg-slate-100 text-slate-700";
}

function sourceLabel(source: string) {
  if (source === "merged") return "已补淘宝信息";
  if (source === "score_db") return "评分库";
  if (source === "taobao") return "淘宝待评分";
  return source;
}

function sourceTone(source: string) {
  if (source === "merged") return "bg-indigo-50 text-indigo-700";
  if (source === "score_db") return "bg-slate-100 text-slate-700";
  return "bg-amber-50 text-amber-700";
}

function SectionTitle(props: { marker: string; title: string; hint?: string }) {
  return (
    <div className="mb-4 flex flex-col gap-1 md:flex-row md:items-center md:gap-3">
      <div className="flex items-center gap-2">
        <span className="flex h-6 min-w-6 items-center justify-center rounded-md bg-slate-900 px-2 text-xs font-semibold text-white">
          {props.marker}
        </span>
        <h2 className="text-base font-semibold text-slate-950">{props.title}</h2>
      </div>
      {props.hint && <p className="text-xs font-medium text-slate-500">{props.hint}</p>}
    </div>
  );
}

function productDisplayName(product?: ProductInfo) {
  if (!product) return "";
  return [product.brand_name, product.name].filter(Boolean).join(" ");
}

function MultiSelect(props: {
  label: string;
  description: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <div className="text-base font-semibold text-slate-950">{props.label}</div>
          <p className="mt-1 text-sm leading-5 text-slate-500">{props.description}</p>
        </div>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
          {props.selected.length} 项
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {props.options.map((option) => {
          const active = props.selected.includes(option);
          return (
            <button
              key={option}
              type="button"
              onClick={() => props.onChange(normalizeMultiSelect(props.options, props.selected, option))}
              className={`rounded-full border px-3 py-2 text-sm transition ${
                active
                  ? "border-slate-900 bg-slate-900 text-white"
                  : "border-slate-200 bg-slate-50 text-slate-700 hover:border-slate-300 hover:bg-white"
              }`}
            >
              {option}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function InlineMultiSelect(props: {
  label: string;
  note: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2 md:flex-row md:items-center">
      <div className="shrink-0 text-sm font-semibold text-slate-950 md:w-32">
        {props.label} <span className="ml-1 text-xs font-medium text-slate-400">（{props.note}）</span>
      </div>
      <div className="flex min-w-0 flex-wrap gap-2">
        {props.options.map((option) => {
          const active = props.selected.includes(option);
          return (
            <button
              key={option}
              type="button"
              onClick={() => props.onChange(normalizeMultiSelect(props.options, props.selected, option))}
              className={`inline-flex h-9 items-center rounded-full border px-4 text-xs font-medium transition ${
                active
                  ? "border-slate-700 bg-slate-800 text-white shadow-sm"
                  : "border-slate-200 bg-white text-slate-500 hover:border-blue-200 hover:bg-blue-50"
              }`}
            >
              {option}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ProductCombobox(props: {
  label: string;
  value: string;
  options: string[];
  placeholder: string;
  emptyText?: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(props.value);

  useEffect(() => {
    setQuery(props.value);
  }, [props.value]);

  const filteredOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) return props.options.slice(0, 30);
    return props.options
      .filter((option) => option.toLowerCase().includes(normalizedQuery))
      .slice(0, 30);
  }, [props.options, query]);

  function selectOption(option: string) {
    props.onChange(option);
    setQuery(option);
    setOpen(false);
  }

  return (
    <div className="relative">
      <label className="mb-1 block text-sm font-medium text-slate-700">{props.label}</label>
      <input
        value={query}
        onFocus={() => setOpen(true)}
        onChange={(event) => {
          const next = event.target.value;
          setQuery(next);
          props.onChange(next);
          setOpen(true);
        }}
        onBlur={() => {
          window.setTimeout(() => setOpen(false), 120);
        }}
        className="h-[42px] w-full rounded-xl border border-slate-200 bg-white px-3 text-sm outline-none focus:border-slate-900"
        placeholder={props.placeholder}
      />

      {open && (
        <div className="absolute z-30 mt-2 max-h-80 w-full overflow-auto rounded-2xl border border-slate-200 bg-white p-2 shadow-xl">
          {filteredOptions.length ? (
            filteredOptions.map((option) => (
              <button
                key={option}
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectOption(option)}
                className={`mb-1 w-full rounded-xl px-3 py-2 text-left text-sm ${
                  option === props.value
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-50"
                }`}
              >
                {option}
              </button>
            ))
          ) : (
            <div className="px-3 py-6 text-center text-sm text-slate-500">{props.emptyText || "没有匹配的产品"}</div>
          )}
        </div>
      )}
    </div>
  );
}

function ProductPicker(props: {
  label: string;
  value: string;
  options: ProductOption[];
  loading: boolean;
  onChange: (value: string, option?: ProductOption) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [origin, setOrigin] = useState("全部");
  const [priceBucket, setPriceBucket] = useState("全部");
  const [functionTag, setFunctionTag] = useState("全部");
  const [onlyAvailable, setOnlyAvailable] = useState(true);

  const selected = useMemo(() => {
    return props.options.find((option) => option.label === props.value || option.product_key === props.value);
  }, [props.options, props.value]);

  const filteredOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return props.options
      .filter((option) => {
        if (onlyAvailable && !option.compare_available) return false;
        if (origin !== "全部" && option.origin_type !== origin) return false;
        if (priceBucket !== "全部" && option.price_bucket !== priceBucket) return false;
        if (
          functionTag !== "全部" &&
          !(option.function_tags || []).some((item) => item.tag === functionTag || item.display_tag === functionTag)
        ) {
          return false;
        }
        if (!normalizedQuery) return true;
        const haystack = [
          option.label,
          option.brand,
          option.product_name,
          option.raw_title,
          option.food_taste,
          option.net_content,
          option.display_text,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return haystack.includes(normalizedQuery);
      })
      .slice(0, 40);
  }, [functionTag, onlyAvailable, origin, priceBucket, props.options, query]);

  const filters = [
    { value: "全部", setter: setOrigin, active: origin === "全部" },
    { value: "国产品牌", label: "国产", setter: setOrigin, active: origin === "国产品牌" },
    { value: "进口/国际品牌", label: "进口", setter: setOrigin, active: origin === "进口/国际品牌" },
    { value: "<50", setter: setPriceBucket, active: priceBucket === "<50" },
    { value: "50-80", setter: setPriceBucket, active: priceBucket === "50-80" },
    { value: "80以上", setter: setPriceBucket, active: priceBucket === "80以上" },
    { value: "肠胃友好", setter: setFunctionTag, active: functionTag === "肠胃友好" },
    { value: "黑下巴友好", setter: setFunctionTag, active: functionTag === "黑下巴友好" },
    { value: "控重管理", setter: setFunctionTag, active: functionTag === "控重管理" },
    { value: "皮肤毛发", setter: setFunctionTag, active: functionTag === "皮肤毛发" },
    { value: "增肌长肉", setter: setFunctionTag, active: functionTag === "增肌长肉" },
  ];

  function selectOption(option: ProductOption) {
    if (!option.compare_available) return;
    props.onChange(option.label, option);
    setQuery("");
    setOpen(false);
  }

  function clearSelection() {
    props.onChange("");
    setQuery("");
    setOpen(false);
  }

  const displayValue = open ? query : selected?.label || props.value;

  return (
    <div className="relative">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-950">
        <span>{props.label}</span>
        <span className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] text-slate-400">i</span>
      </div>

      <div className="relative">
        <span className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-lg text-slate-400">⌕</span>
        <input
          value={displayValue}
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onBlur={() => window.setTimeout(() => setOpen(false), 150)}
          className="h-12 w-full rounded-xl border border-slate-200 bg-white pl-11 pr-20 text-sm font-medium text-slate-950 outline-none transition focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
          placeholder="输入品牌或产品名"
        />
        {(selected || displayValue) && (
          <button
            type="button"
            onMouseDown={(event) => event.preventDefault()}
            onClick={clearSelection}
            className="absolute right-12 top-1/2 -translate-y-1/2 text-lg text-slate-400 hover:text-slate-700"
            aria-label="清空"
          >
            ×
          </button>
        )}
        <button
          type="button"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => setOpen((prev) => !prev)}
          className="absolute right-4 top-1/2 -translate-y-1/2 text-lg text-slate-700"
          aria-label="搜索"
        >
          ⌄
        </button>
      </div>

      {open && (
        <div className="absolute z-40 mt-2 w-full overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
          <div className="border-b border-slate-100 p-3">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="text-xs font-semibold text-slate-500">筛选产品目录</div>
              <label className="flex items-center gap-2 text-xs text-slate-500">
                <input
                  type="checkbox"
                  checked={onlyAvailable}
                  onChange={(event) => setOnlyAvailable(event.target.checked)}
                  className="h-3.5 w-3.5 rounded border-slate-300"
                />
                仅可对比
              </label>
            </div>
            <div className="flex flex-wrap gap-2">
              {filters.map((filter) => (
                <button
                  key={`${filter.value}-${filter.label || ""}`}
                  type="button"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => filter.setter(filter.value)}
                  className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                    filter.active ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  {filter.label || filter.value}
                </button>
              ))}
            </div>
          </div>
          <div className="max-h-[360px] overflow-auto">
            {props.loading ? (
              <div className="px-4 py-8 text-center text-sm text-slate-500">正在加载产品目录...</div>
            ) : filteredOptions.length ? (
              filteredOptions.map((option) => {
                const disabled = !option.compare_available;
                return (
                  <button
                    key={option.catalog_key}
                    type="button"
                    disabled={disabled}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => selectOption(option)}
                    className={`flex w-full items-center gap-3 border-b border-slate-100 px-4 py-3 text-left last:border-b-0 ${
                      option.label === props.value ? "bg-blue-50" : "bg-white hover:bg-slate-50"
                    } ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
                  >
                    {option.main_image_url ? (
                      <img src={option.main_image_url} alt="" className="h-12 w-12 shrink-0 rounded-full object-cover" />
                    ) : (
                      <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-blue-50 text-xs text-blue-400">·</div>
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-semibold text-slate-950">{option.label}</div>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        <span className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${optionTone(option.origin_type)}`}>
                          {option.origin_type?.includes("进口") ? "进口" : option.origin_type?.includes("国产") ? "国产" : "待确认"}
                        </span>
                        {option.price_bucket && option.price_bucket !== "未知" && (
                          <span className="rounded-md bg-orange-50 px-2 py-0.5 text-[11px] font-medium text-orange-700">{option.price_bucket}</span>
                        )}
                        {(option.function_tags || []).slice(0, 1).map((tag) => (
                          <span key={`${option.catalog_key}-${tag.tag}`} className="rounded-md bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                            {tag.display_tag || tag.tag}
                          </span>
                        ))}
                        {disabled && <span className="rounded-md bg-rose-50 px-2 py-0.5 text-[11px] font-medium text-rose-700">暂无评分</span>}
                      </div>
                    </div>
                  </button>
                );
              })
            ) : (
              <div className="px-4 py-8 text-center text-sm text-slate-500">没有匹配的产品</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function SelectedFoodSummary(props: {
  label: string;
  value: string;
  option?: ProductOption;
  accent: "blue" | "orange";
}) {
  const iconClass = props.accent === "blue" ? "border-blue-200 bg-blue-50 text-blue-600" : "border-orange-200 bg-orange-50 text-orange-600";
  const title = props.option?.label || props.value || "未选择";
  const brand = props.option?.brand || props.value.split(" ")[0] || "待选择";
  const tags = [
    `品牌：${brand}`,
    props.option?.origin_type?.includes("进口") ? "进口" : props.option?.origin_type?.includes("国产") ? "国产" : "",
    props.option?.price_bucket && props.option.price_bucket !== "未知" ? `${props.option.price_bucket}元/斤` : "",
    ...(props.option?.function_tags || []).slice(0, 1).map((tag) => tag.display_tag || tag.tag),
  ].filter(Boolean);

  return (
    <div className="flex min-w-0 items-center gap-3">
      <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border ${iconClass}`}>
        <span className="h-4 w-5 rounded-b-lg rounded-t-sm border-2 border-current" />
      </div>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <span className="shrink-0 font-semibold text-slate-700">{props.label}：</span>
          <span className="truncate font-bold text-slate-950">{title}</span>
        </div>
        <div className="mt-1 flex min-w-0 flex-wrap gap-2">
          {tags.length ? (
            tags.map((tag) => (
              <span key={tag} className="rounded-md bg-slate-100 px-2.5 py-1 text-[11px] font-medium text-slate-600">
                {tag}
              </span>
            ))
          ) : (
            <span className="rounded-md bg-slate-100 px-2.5 py-1 text-[11px] font-medium text-slate-500">请选择产品</span>
          )}
        </div>
      </div>
    </div>
  );
}

function MissingProductUploadEntry(props: {
  submitted?: MissingProductSubmission;
  onOpen: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-center">
        <div>
          <h3 className="font-semibold text-slate-950">产品库中不存在此商品？</h3>
          <p className="mt-1 text-sm text-slate-500">
            请上传需要分析的配料表图片。
          </p>
          {props.submitted && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-slate-600">
              <span className="rounded-full bg-white px-3 py-1">
                品牌：{props.submitted.brandName}
              </span>
              <span className="rounded-full bg-white px-3 py-1">
                产品：{props.submitted.productName}
              </span>
              <span className="rounded-full bg-white px-3 py-1">
                图片：{props.submitted.fileName}
              </span>
              <button type="button" onClick={props.onRemove} className="text-xs text-slate-500 underline">
                移除
              </button>
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={props.onOpen}
          className="shrink-0 rounded-full bg-slate-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-800"
        >
          {props.submitted ? "重新提交图片" : "上传需要分析的图片"}
        </button>
      </div>
    </div>
  );
}

function MissingProductUploadModal(props: {
  open: boolean;
  brandOptions: string[];
  onClose: () => void;
  onSubmit: (submission: MissingProductSubmission) => void;
}) {
  const [brandName, setBrandName] = useState("");
  const [productName, setProductName] = useState("");
  const [image, setImage] = useState<UploadedImage | undefined>();

  if (!props.open) return null;

  function handleFile(file?: File) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      window.alert("请上传图片格式的配料表，例如 jpg、png、webp。");
      return;
    }
    if (image?.previewUrl) URL.revokeObjectURL(image.previewUrl);
    setImage({
      fileName: file.name,
      previewUrl: URL.createObjectURL(file),
      file,
    });
  }

  function handleClose() {
    setBrandName("");
    setProductName("");
    if (image?.previewUrl) URL.revokeObjectURL(image.previewUrl);
    setImage(undefined);
    props.onClose();
  }

  function handleSubmit() {
    if (!brandName.trim()) {
      window.alert("请选择或输入品牌。");
      return;
    }
    if (!productName.trim()) {
      window.alert("请输入产品名称。");
      return;
    }
    if (!image) {
      window.alert("请上传需要分析的图片。");
      return;
    }
    const normalizedBrand = brandName.trim();
    const normalizedProduct = productName.trim();
    const fullProductName = normalizedProduct.startsWith(normalizedBrand)
      ? normalizedProduct
      : `${normalizedBrand} ${normalizedProduct}`;
    props.onSubmit({
      brandName: normalizedBrand,
      productName: fullProductName,
      fileName: image.fileName,
      previewUrl: image.previewUrl,
      file: image.file,
    });
    setBrandName("");
    setProductName("");
    setImage(undefined);
    props.onClose();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div className="w-full max-w-xl rounded-3xl bg-white p-6 shadow-xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-semibold text-slate-950">上传需要分析的图片</h3>
            <p className="mt-1 text-sm leading-6 text-slate-500">
              当产品库中不存在该商品时，填写产品名称并上传配料表图片。
            </p>
          </div>
          <button type="button" onClick={handleClose} className="rounded-full bg-slate-100 px-3 py-1 text-sm text-slate-600">
            关闭
          </button>
        </div>

        <div className="mt-5">
          <ProductCombobox
            label="品牌"
            value={brandName}
            options={props.brandOptions}
            placeholder="输入品牌名搜索，例如 好主人、皇家、GO!"
            emptyText="没有匹配的品牌，可直接输入新品牌"
            onChange={setBrandName}
          />
        </div>

        <div className="mt-4">
          <label className="mb-1 block text-sm font-medium text-slate-700">产品名称</label>
          <input
            value={productName}
            onChange={(event) => setProductName(event.target.value)}
            className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-slate-900"
            placeholder="请输入产品名，例如 金装无谷成猫粮 鸡鱼配方"
          />
        </div>

        <div className="mt-5">
          <label className="mb-1 block text-sm font-medium text-slate-700">配料表图片</label>
          <label className="flex min-h-44 cursor-pointer flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-center text-sm text-slate-500 hover:bg-slate-100">
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(event) => {
                handleFile(event.target.files?.[0]);
                event.currentTarget.value = "";
              }}
            />
            {image ? (
              <span>已选择：{image.fileName}</span>
            ) : (
              <span>点击选择图片，支持 jpg、png、webp</span>
            )}
          </label>
          {image && (
            <img
              src={image.previewUrl}
              alt="配料表预览"
              className="mt-3 max-h-64 w-full rounded-2xl bg-slate-50 object-contain"
            />
          )}
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={handleClose}
            className="rounded-full border border-slate-200 px-5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            className="rounded-full bg-slate-900 px-5 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            提交
          </button>
        </div>
      </div>
    </div>
  );
}

function TaskStatusPanel(props: { title: string; task: CatFoodTask | null; error: string; loading: boolean }) {
  const task = props.task;
  return (
    <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <div>
          <div className="text-sm font-semibold text-slate-800">{props.title}</div>
          <p className="mt-1 text-sm text-slate-500">
            {task
              ? `任务 ${task.id.slice(0, 8)}｜${task.status}｜进度 ${task.progress}%`
              : "尚未创建任务。"}
          </p>
        </div>
        <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600">
          {props.loading ? "处理中" : task ? task.task_type : "未创建"}
        </span>
      </div>
      {task && (
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-white">
          <div
            className="h-full rounded-full bg-slate-900 transition-all"
            style={{ width: `${Math.max(0, Math.min(100, task.progress || 0))}%` }}
          />
        </div>
      )}
      {task?.images?.length ? (
        <div className="mt-3 space-y-2">
          {task.images.map((image) => (
            <div key={image.id} className="rounded-xl bg-white px-3 py-2 text-xs leading-5 text-slate-600">
              <span className="font-semibold text-slate-800">{image.original_filename}</span>
              <span> ｜解析状态：{image.parse_status}</span>
              {image.parse_result?.message && <span> ｜{image.parse_result.message}</span>}
            </div>
          ))}
        </div>
      ) : null}
      {(props.error || task?.error_message) && (
        <div className="mt-3 rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {props.error || task?.error_message}
        </div>
      )}
    </div>
  );
}

function OrchestratorStatusPanel(props: { title: string; task: OrchestratorTask | null }) {
  const task = props.task;
  if (!task) return null;

  return (
    <div className="mt-3 rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-col justify-between gap-2 md:flex-row md:items-center">
        <div>
          <div className="text-sm font-semibold text-slate-800">{props.title}</div>
          <p className="mt-1 text-xs text-slate-500">
            编排任务 {task.id.slice(0, 8)}｜{task.task_type}｜{task.task_status}
          </p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-600">
          {task.nodes.length} 个节点
        </span>
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-2">
        {task.nodes.map((node) => (
          <div key={node.node_code} className="rounded-xl bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
            <div className="font-semibold text-slate-800">{node.node_name}</div>
            <div>
              {node.node_code} ｜ {node.node_status}
            </div>
            {node.error_message && <div className="text-rose-600">{node.error_message}</div>}
          </div>
        ))}
      </div>
      {task.error_message && (
        <div className="mt-3 rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {task.error_message}
        </div>
      )}
    </div>
  );
}

function RadarChart(props: { title: string; profile: ProfilePoint[]; baseline?: ProfilePoint[]; color: string }) {
  const size = 340;
  const center = size / 2;
  const radius = 112;
  const profile = props.profile.filter((item) => item.score !== null && item.score !== undefined);

  const axisPoints = profile.map((item, index) => {
    const angle = (Math.PI * 2 * index) / profile.length - Math.PI / 2;
    const x = center + Math.cos(angle) * radius;
    const y = center + Math.sin(angle) * radius;
    const labelX = center + Math.cos(angle) * (radius + 30);
    const labelY = center + Math.sin(angle) * (radius + 30);
    const valueRadius = radius * Math.max(0, Math.min(100, Number(item.score))) / 100;
    return {
      ...item,
      scoreValue: Number(item.score),
      angle,
      x,
      y,
      labelX,
      labelY,
      valueX: center + Math.cos(angle) * valueRadius,
      valueY: center + Math.sin(angle) * valueRadius,
      scoreLabelX: center + Math.cos(angle) * (valueRadius + 18),
      scoreLabelY: center + Math.sin(angle) * (valueRadius + 18),
    };
  });

  const polygon = axisPoints.map((point) => `${point.valueX},${point.valueY}`).join(" ");
  const baselineMap = new Map((props.baseline || []).map((item) => [item.dimension, item.score]));
  const baselinePointObjects = axisPoints
    .map((point, index) => {
      const score = baselineMap.get(point.dimension);
      if (score === null || score === undefined || Number.isNaN(Number(score))) return null;
      const angle = (Math.PI * 2 * index) / axisPoints.length - Math.PI / 2;
      const valueRadius = radius * Math.max(0, Math.min(100, Number(score))) / 100;
      return {
        dimension: point.dimension,
        score: Number(score),
        x: center + Math.cos(angle) * valueRadius,
        y: center + Math.sin(angle) * valueRadius,
        labelX: center + Math.cos(angle) * (valueRadius + 10),
        labelY: center + Math.sin(angle) * (valueRadius + 10),
      };
    })
    .filter(Boolean);
  const baselinePoints = baselinePointObjects.map((point) => `${point?.x},${point?.y}`);
  const baselinePolygon =
    baselinePoints.length === axisPoints.length ? [...baselinePoints, baselinePoints[0]].join(" ") : "";
  const gridLevels = [0.25, 0.5, 0.75, 1];
  const hasData = axisPoints.length >= 3;

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="font-semibold text-slate-950">{props.title}</h3>
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600">0-100</span>
          <span className="inline-flex items-center gap-1 text-xs text-slate-500">
            <span className="h-px w-5 border-t border-dashed border-slate-400" />
            基线
          </span>
        </div>
      </div>

      {!hasData ? (
        <div className="flex h-[340px] items-center justify-center rounded-xl bg-slate-50 text-sm text-slate-500">
          暂无足够 score 数据生成雷达图
        </div>
      ) : (
        <svg viewBox={`0 0 ${size} ${size}`} className="h-[340px] w-full">
          {gridLevels.map((level) => {
            const points = axisPoints
              .map((_, index) => {
                const angle = (Math.PI * 2 * index) / axisPoints.length - Math.PI / 2;
                return `${center + Math.cos(angle) * radius * level},${center + Math.sin(angle) * radius * level}`;
              })
              .join(" ");
            return <polygon key={level} points={points} fill="none" stroke="#e2e8f0" strokeWidth="1" />;
          })}

          {axisPoints.map((point) => (
            <line key={point.dimension} x1={center} y1={center} x2={point.x} y2={point.y} stroke="#e2e8f0" />
          ))}

          <polygon points={polygon} fill={props.color} fillOpacity="0.18" stroke={props.color} strokeWidth="2" />
          {baselinePolygon && (
            <polygon points={baselinePolygon} fill="none" stroke="#64748b" strokeDasharray="5 5" strokeWidth="2" />
          )}
          {baselinePointObjects.map((point) =>
            point ? (
              <text
                key={`${point.dimension}-baseline-score`}
                x={point.labelX}
                y={point.labelY}
                textAnchor={point.labelX < center - 6 ? "end" : point.labelX > center + 6 ? "start" : "middle"}
                dominantBaseline="middle"
                className="fill-slate-500 text-[10px]"
              >
                {formatScore(point.score)}
              </text>
            ) : null
          )}
          {axisPoints.map((point) => (
            <g key={point.dimension}>
              <circle cx={point.valueX} cy={point.valueY} r="4" fill={props.color} />
              <text
                x={point.scoreLabelX}
                y={point.scoreLabelY}
                textAnchor={point.scoreLabelX < center - 6 ? "end" : point.scoreLabelX > center + 6 ? "start" : "middle"}
                dominantBaseline="middle"
                className="text-[12px] font-bold"
                fill={props.color}
              >
                {formatScore(point.scoreValue)}
              </text>
              <text
                x={point.labelX}
                y={point.labelY}
                textAnchor={point.labelX < center - 10 ? "end" : point.labelX > center + 10 ? "start" : "middle"}
                dominantBaseline="middle"
                className="fill-slate-600 text-[11px]"
              >
                {point.dimension}
              </text>
            </g>
          ))}
        </svg>
      )}
    </div>
  );
}

function focusIcon(dimension: string) {
  if (dimension.includes("蛋白")) return "♙";
  if (dimension.includes("脂肪") || dimension.includes("黑下巴")) return "◉";
  if (dimension.includes("肠胃") || dimension.includes("纤维") || dimension.includes("菌群")) return "♧";
  if (dimension.includes("Omega") || dimension.includes("皮肤")) return "⌘";
  return "◎";
}

function FocusBarComparison(props: {
  currentProfile: ProfilePoint[];
  targetProfile: ProfilePoint[];
  currentName: string;
  targetName: string;
}) {
  const targetScoreByDimension = new Map(
    props.targetProfile
      .filter((point) => point.score !== null && point.score !== undefined)
      .map((point) => [point.dimension, Number(point.score)]),
  );
  const rows = props.currentProfile
    .filter((point) => point.score !== null && point.score !== undefined && targetScoreByDimension.has(point.dimension))
    .map((point) => ({
      dimension: point.dimension,
      currentScore: Number(point.score),
      targetScore: Number(targetScoreByDimension.get(point.dimension)),
    }))
    .slice(0, 7);

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="mb-3 flex justify-end">
        <div className="flex flex-wrap items-center gap-4 text-xs font-medium text-slate-500">
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full bg-blue-600" />
            {props.currentName}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full bg-emerald-500" />
            {props.targetName}
          </span>
          <span>0–100 分</span>
        </div>
      </div>

      {rows.length ? (
        <div className="space-y-3">
          {rows.map((row) => {
            const currentScore = Math.max(0, Math.min(100, row.currentScore));
            const targetScore = Math.max(0, Math.min(100, row.targetScore));
            return (
              <div key={row.dimension} className="grid gap-2 md:grid-cols-[140px_1fr_1fr] md:items-center">
                <div className="flex min-w-0 items-center gap-2 text-xs font-medium text-slate-700">
                  <span className="text-slate-400">{focusIcon(row.dimension)}</span>
                  <span className="truncate">{row.dimension}</span>
                </div>
                <div className="flex items-center gap-2">
                  <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                    <div className="h-full rounded-full bg-blue-600" style={{ width: `${currentScore}%` }} />
                  </div>
                  <span className="w-8 text-right text-xs font-medium text-slate-700">{formatScore(currentScore)}</span>
                </div>
                <div className="flex items-center gap-2">
                  <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                    <div className="h-full rounded-full bg-emerald-500" style={{ width: `${targetScore}%` }} />
                  </div>
                  <span className="w-8 text-right text-xs font-medium text-slate-700">{formatScore(targetScore)}</span>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="rounded-xl bg-slate-50 px-4 py-6 text-center text-sm text-slate-500">
          暂无足够分数生成侧重点对比。
        </div>
      )}
    </div>
  );
}

function keyChangeSummary(row: ProfileDiff, currentName: string, targetName: string) {
  if (row.diff_b_minus_a === null || row.diff_b_minus_a === undefined) return row.summary || "该维度暂无足够数据。";
  const config = DIMENSION_INTERPRETATION_CONFIG[row.dimension];
  const delta = row.diff_b_minus_a;
  const higher = delta > 0 ? targetName : currentName;
  const lower = delta > 0 ? currentName : targetName;
  const displayName = config?.displayName || row.dimension;
  const absDelta = Math.abs(delta).toFixed(0);
  if ((config?.type || row.type) === "pressure") {
    return `${displayName}：${higher} 比 ${lower} 高 ${absDelta} 分，需要结合猫咪耐受观察。`;
  }
  return `${displayName}：${higher} 比 ${lower} 高 ${absDelta} 分，相关支持表现更突出。`;
}

function KeyChangeSummary(props: {
  rows: ProfileDiff[];
  currentName: string;
  targetName: string;
}) {
  const rows = props.rows.slice(0, 4);

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <h3 className="mb-3 text-sm font-semibold text-slate-950">关键变化总结</h3>
      {rows.length ? (
        <div className="space-y-3">
          {rows.map((row) => {
            const isPositive =
              row.diff_b_minus_a !== null &&
              row.diff_b_minus_a !== undefined &&
              ((row.type === "pressure" && row.diff_b_minus_a < 0) || (row.type !== "pressure" && row.diff_b_minus_a > 0));
            return (
              <div key={row.dimension} className="flex gap-2 text-sm leading-5 text-slate-700">
                <span
                  className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] ${
                    isPositive ? "border-emerald-300 bg-emerald-50 text-emerald-600" : "border-rose-300 bg-rose-50 text-rose-600"
                  }`}
                >
                  {isPositive ? "↑" : "!"}
                </span>
                <span>{keyChangeSummary(row, props.currentName, props.targetName)}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="rounded-xl bg-slate-50 px-4 py-6 text-center text-sm text-slate-500">
          两款粮暂无明显关键变化。
        </div>
      )}
    </div>
  );
}

function dimensionMeta(dimension: string) {
  if (dimension.includes("黑下巴")) {
    return {
      icon: "◉",
      tone: "bg-rose-50 text-rose-600",
      description: "反映配方对黑下巴问题的潜在影响",
    };
  }
  if (dimension.includes("肠胃")) {
    return {
      icon: "♨",
      tone: "bg-orange-50 text-orange-600",
      description: "反映配方对肠胃健康的潜在影响",
    };
  }
  if (dimension.includes("适口")) {
    return {
      icon: "▱",
      tone: "bg-slate-100 text-slate-500",
      description: "反映猫咪对口味和适口性的潜在接受度",
    };
  }
  if (dimension.includes("皮肤")) {
    return {
      icon: "✦",
      tone: "bg-emerald-50 text-emerald-600",
      description: "反映配方对皮肤和毛发状态的潜在影响",
    };
  }
  return {
    icon: "◎",
    tone: "bg-blue-50 text-blue-600",
    description: "反映该维度在整体产品池中的相对表现",
  };
}

function scoreLevelLabel(score: number | null | undefined) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) return "暂无数据";
  if (Number(score) >= 66) return "中高";
  if (Number(score) >= 40) return "中等";
  return "偏低";
}

function scorePositionLabel(score: number | null | undefined) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) return "暂无足够数据支持横向评估";
  if (Number(score) >= 66) return "处于中游偏上";
  if (Number(score) >= 40) return "低于多数产品";
  return "处于靠后位置";
}

function DimensionScoreBlock(props: {
  label: string;
  name: string;
  score: number | null;
  accent: "blue" | "green";
}) {
  const hasData = props.score !== null && props.score !== undefined && !Number.isNaN(Number(props.score));
  const score = hasData ? Math.max(0, Math.min(100, Number(props.score))) : 0;
  const accentClass = props.accent === "blue" ? "text-blue-600" : "text-emerald-600";
  const barClass = props.accent === "blue" ? "bg-blue-600" : "bg-emerald-500";
  const badgeClass = hasData && score >= 66 ? "bg-emerald-50 text-emerald-700" : hasData ? "bg-rose-50 text-rose-600" : "bg-slate-100 text-slate-500";

  return (
    <div className="min-w-0">
      <div className="mb-2 flex min-w-0 items-center gap-2 text-xs">
        <span className={`shrink-0 font-semibold ${accentClass}`}>{props.label}</span>
        <span className="text-slate-300">|</span>
        <span className="truncate font-medium text-slate-500">{props.name}</span>
      </div>
      {hasData ? (
        <>
          <div className="flex items-center gap-2">
            <span className="text-base font-semibold leading-none text-slate-950">{Number(props.score).toFixed(1)}</span>
            <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${badgeClass}`}>{scoreLevelLabel(props.score)}</span>
          </div>
          <div className="mt-2 text-xs font-medium text-slate-500">{scorePositionLabel(props.score)}</div>
          <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
            <div className={`h-full rounded-full ${barClass}`} style={{ width: `${score}%` }} />
          </div>
          <div className="mt-2 flex justify-between text-xs font-medium text-slate-500">
            <span>低</span>
            <span>中</span>
            <span>高</span>
          </div>
        </>
      ) : (
        <div className="rounded-xl bg-slate-50 px-4 py-5 text-xs font-medium text-slate-500">
          暂无数据
          <div className="mt-2 text-xs font-normal">当前暂无足够数据支持横向评估</div>
        </div>
      )}
    </div>
  );
}

function DimensionComparisonPanel(props: {
  rows: FriendlyRow[];
  currentName: string;
  targetName: string;
}) {
  return (
    <section className="mt-4 rounded-2xl border border-slate-200 bg-white p-4">
      <div className="mb-3 flex flex-col justify-end gap-3 md:flex-row md:items-center">
        <div className="flex flex-wrap items-center gap-4 text-xs font-normal text-slate-500">
          <span className="inline-flex items-center gap-1.5"><span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-50 text-xs text-emerald-600">↑</span>对比粮更优</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-full bg-blue-100 ring-4 ring-blue-50" />当前粮更优</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-full bg-slate-300 ring-4 ring-slate-100" />两者接近</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-full bg-orange-100 ring-4 ring-orange-50" />暂无数据</span>
        </div>
      </div>

      <div className="space-y-3">
        {props.rows.map((row) => {
          const meta = dimensionMeta(row.dimension);
          const currentScore = row.current_score;
          const targetScore = row.target_score;
          const hasBoth = currentScore !== null && currentScore !== undefined && targetScore !== null && targetScore !== undefined;
          const diff = hasBoth ? Number(targetScore) - Number(currentScore) : null;
          const status =
            diff === null
              ? { label: "暂无数据", tone: "bg-slate-100 text-slate-500", icon: "−", text: "暂不支持该维度横向评估" }
              : Math.abs(diff) < 5
                ? { label: "两者接近", tone: "bg-slate-100 text-slate-600", icon: "−", text: `两款粮在${row.dimension}上表现接近。` }
                : diff > 0
                  ? { label: "对比粮更优", tone: "bg-emerald-50 text-emerald-700", icon: "↑", text: `对比粮领先 ${Math.abs(diff).toFixed(1)} 分，在产品库中更靠前，${row.dimension}优势更明显。` }
                  : { label: "当前粮更优", tone: "bg-blue-50 text-blue-700", icon: "↑", text: `当前粮领先 ${Math.abs(diff).toFixed(1)} 分，在${row.dimension}上优势更明显。` };

          return (
            <div key={row.dimension} className="grid overflow-hidden rounded-2xl border border-slate-200 bg-white lg:grid-cols-[250px_1fr_1fr_250px]">
              <div className="flex items-center gap-3 border-b border-slate-100 bg-slate-50/40 p-4 lg:border-b-0 lg:border-r">
                <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-base ${meta.tone}`}>{meta.icon}</div>
                <div>
                  <div className="text-sm font-semibold text-slate-950">{row.dimension}</div>
                  <div className="mt-1.5 text-xs leading-5 text-slate-500">{meta.description}</div>
                </div>
              </div>
              <div className="border-b border-slate-100 p-4 lg:border-b-0 lg:border-r">
                <DimensionScoreBlock label="当前粮" name={props.currentName} score={currentScore} accent="blue" />
              </div>
              <div className="relative border-b border-slate-100 p-4 lg:border-b-0 lg:border-r">
                <span className="absolute -left-3.5 top-1/2 hidden h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full bg-slate-100 text-[11px] font-semibold text-slate-500 lg:flex">VS</span>
                <DimensionScoreBlock label="对比粮" name={props.targetName} score={targetScore} accent="green" />
              </div>
              <div className="flex items-center justify-between gap-3 p-4">
                <div>
                  <span className={`rounded-lg px-2.5 py-1 text-xs font-semibold ${status.tone}`}>{status.label}</span>
                  <div className="mt-3 text-xs leading-5 text-slate-600">{status.text}</div>
                </div>
                <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-base ${status.tone}`}>{status.icon}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-4 flex items-center gap-3 rounded-xl border border-blue-100 bg-blue-50 px-4 py-3 text-xs leading-5 text-slate-600">
        <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-blue-500 text-[10px] font-bold text-white">i</span>
        <span><span className="font-semibold text-slate-800">说明</span>　以上结果基于产品库样本数据进行风险换算和横向排序，仅作为参考，不代表绝对结论，建议结合猫咪个体情况综合评估。</span>
      </div>
    </section>
  );
}

function ProductIngredientCard(props: {
  label: string;
  product: ProductInfo;
  option?: ProductOption;
}) {
  const [expanded, setExpanded] = useState(false);
  const ingredientText = props.product.ingredient_composition || "暂无原始配料信息";
  const collapsedText = ingredientText.length > 150 ? `${ingredientText.slice(0, 150)}...` : ingredientText;
  const brand = props.option?.brand || props.product.brand_name || "待确认";
  const origin = props.option?.origin_type?.includes("进口")
    ? "进口品牌"
    : props.option?.origin_type?.includes("国产")
      ? "国产品牌"
      : "";
  const price = props.option?.price_bucket && props.option.price_bucket !== "未知" ? `${props.option.price_bucket}元/斤` : "";
  const meta = [`品牌：${brand}`, origin, price].filter(Boolean);

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="text-sm font-bold leading-snug text-slate-950">
        {props.label}｜{productDisplayName(props.product)}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs font-medium text-slate-500">
        {meta.map((item, index) => (
          <React.Fragment key={item}>
            {index > 0 && <span className="text-slate-300">|</span>}
            <span>{item}</span>
          </React.Fragment>
        ))}
      </div>
      <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
        <div className="mb-1 text-xs font-bold text-slate-700">原始配料信息</div>
        <div className="text-xs leading-5 text-slate-600">
          {expanded ? ingredientText : collapsedText}
        </div>
        {ingredientText.length > 150 && (
          <button
            type="button"
            onClick={() => setExpanded((prev) => !prev)}
            className="mx-auto mt-2 block text-xs font-medium text-blue-600 hover:text-blue-500"
          >
            {expanded ? "收起" : "展开全部"}⌄
          </button>
        )}
      </div>
    </div>
  );
}

export default function CatFoodComparePage() {
  const [productOptions, setProductOptions] = useState<ProductOption[]>([]);
  const [brandOptions, setBrandOptions] = useState<string[]>([]);
  const [productsLoading, setProductsLoading] = useState(true);
  const [productsError, setProductsError] = useState("");
  const [currentFood, setCurrentFood] = useState("");
  const [targetFood, setTargetFood] = useState("");
  const [catProfile, setCatProfile] = useState<CatProfile>({
    age: "3～6年",
    historyIssues: ["无明显历史问题"],
    recentSymptoms: ["无明显异常"],
  });
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState("");
  const [summary, setSummary] = useState("");
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState("");
  const [summaryCached, setSummaryCached] = useState(false);
  const [missingProductModalOpen, setMissingProductModalOpen] = useState(false);
  const [missingProductSubmission, setMissingProductSubmission] = useState<MissingProductSubmission | undefined>();
  const [task, setTask] = useState<CatFoodTask | null>(null);
  const [taskError, setTaskError] = useState("");
  const [imageTask, setImageTask] = useState<CatFoodTask | null>(null);
  const [imageOrchestratorTask, setImageOrchestratorTask] = useState<OrchestratorTask | null>(null);
  const [imageTaskError, setImageTaskError] = useState("");
  const [imageUploading, setImageUploading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function loadProducts() {
      setProductsLoading(true);
      setProductsError("");
      try {
        const [productsResponse, brandsResponse] = await Promise.all([
          fetch("/api/cat-food-compare/product-options?limit=500"),
          fetch("/api/cat-food-compare/brands"),
        ]);
        const data = await productsResponse.json();
        if (!productsResponse.ok) throw new Error(data.error || "产品库加载失败");
        const brandsData = await brandsResponse.json();
        if (!brandsResponse.ok) throw new Error(brandsData.error || "品牌库加载失败");
        if (cancelled) return;
        const nextProducts = data.items || [];
        setProductOptions(nextProducts);
        setBrandOptions(brandsData.brands || []);
      } catch (error) {
        if (!cancelled) {
          setProductsError(error instanceof Error ? error.message : "产品库加载失败");
        }
      } finally {
        if (!cancelled) setProductsLoading(false);
      }
    }
    loadProducts();
    return () => {
      cancelled = true;
    };
  }, []);

  const canCompare = currentFood && targetFood && currentFood !== targetFood && !compareLoading;

  function handleCurrentFoodChange(value: string) {
    setCurrentFood(value);
    setTask(null);
    setCompareResult(null);
    setSummary("");
  }

  function handleTargetFoodChange(value: string) {
    setTargetFood(value);
    setTask(null);
    setCompareResult(null);
    setSummary("");
  }

  function removeMissingProductSubmission() {
    if (missingProductSubmission?.previewUrl) {
      URL.revokeObjectURL(missingProductSubmission.previewUrl);
    }
    setMissingProductSubmission(undefined);
    setImageOrchestratorTask(null);
  }

  function taskPayload() {
    return {
      task_type: "cat_food_compare",
      current_food: currentFood,
      target_food: targetFood,
      cat_profile: catProfile,
    };
  }

  async function createTaskIfNeeded() {
    if (task?.id) return task;
    setTaskError("");
    const response = await fetch("/api/cat-food/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(taskPayload()),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "任务创建失败");
    setTask(data.task);
    return data.task as CatFoodTask;
  }

  async function fetchTask(taskId: string, onTask: (task: CatFoodTask) => void = setTask) {
    const response = await fetch(`/api/cat-food/tasks/${taskId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "任务状态加载失败");
    onTask(data.task);
    return data.task as CatFoodTask;
  }

  async function waitForTask(
    taskId: string,
    pickCompareResult: boolean,
    onTask: (task: CatFoodTask) => void = setTask,
  ) {
    for (let index = 0; index < 60; index += 1) {
      const nextTask = await fetchTask(taskId, onTask);
      if (nextTask.status === "failed") {
        throw new Error(nextTask.error_message || "任务执行失败");
      }
      if (nextTask.status === "success") {
        if (pickCompareResult && nextTask.result?.compare) {
          return nextTask;
        }
        if (!pickCompareResult) return nextTask;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("任务仍在处理中，请稍后刷新任务状态。");
  }

  async function handleMissingProductSubmit(submission: MissingProductSubmission) {
    setImageUploading(true);
    setImageTaskError("");
    try {
      const createResponse = await fetch("/api/cat-food/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: "image_parse",
          cat_profile: catProfile,
          notes: submission.productName,
        }),
      });
      const createData = await createResponse.json();
      if (!createResponse.ok) throw new Error(createData.error || "图片任务创建失败");
      const currentTask = createData.task as CatFoodTask;
      setImageTask(currentTask);

      const form = new FormData();
      form.append("image", submission.file);
      form.append("brand_name", submission.brandName);
      form.append("product_name", submission.productName);
      const response = await fetch(`/api/cat-food/tasks/${currentTask.id}/images`, {
        method: "POST",
        body: form,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "图片上传失败");
      setImageTask(data.task);
      setImageOrchestratorTask(data.orchestrator_task || null);
      setMissingProductSubmission({
        ...submission,
        imageId: data.image?.id,
        orchestratorTaskId: data.orchestrator_task?.id,
      });
      await waitForTask(currentTask.id, false, setImageTask);
    } catch (error) {
      setImageTaskError(error instanceof Error ? error.message : "图片上传失败");
    } finally {
      setImageUploading(false);
    }
  }

  async function handleCompare() {
    if (!canCompare) return;
    setCompareLoading(true);
    setCompareError("");
    setSummary("");
    setSummaryError("");
    setSummaryCached(false);
    try {
      const currentTask = await createTaskIfNeeded();
      const response = await fetch(`/api/cat-food/tasks/${currentTask.id}/compare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_food: currentFood,
          target_food: targetFood,
          cat_profile: catProfile,
          missing_product_upload: missingProductSubmission
            ? {
                product_name: missingProductSubmission.productName,
                file_name: missingProductSubmission.fileName,
              }
            : null,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "对比任务启动失败");
      setTask(data.task);
      const finishedTask = await waitForTask(currentTask.id, true);
      setCompareResult(finishedTask.result?.compare || null);
    } catch (error) {
      setCompareError(error instanceof Error ? error.message : "对比数据生成失败");
      setCompareResult(null);
    } finally {
      setCompareLoading(false);
    }
  }

  async function handleGenerateSummary() {
    if (!compareResult) return;
    setSummaryLoading(true);
    setSummaryError("");
    setSummary("");
    setSummaryCached(false);
    try {
      const currentTask = await createTaskIfNeeded();
      const response = await fetch(`/api/cat-food/tasks/${currentTask.id}/summary`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          llm_context: compareResult.llm_context,
          friendly_rows: compareResult.friendly_rows,
          cat_profile: {
            ...catProfile,
            missing_product_upload: missingProductSubmission
              ? {
                  product_name: missingProductSubmission.productName,
                  file_name: missingProductSubmission.fileName,
                }
              : null,
          },
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "大模型总结生成失败");
      setSummary(data.summary || "");
      setSummaryCached(Boolean(data.cached));
    } catch (error) {
      setSummaryError(error instanceof Error ? error.message : "大模型总结生成失败");
    } finally {
      setSummaryLoading(false);
    }
  }

  const selectedCurrentFood = useMemo(() => {
    return productOptions.find((option) => option.label === currentFood || option.product_key === currentFood);
  }, [currentFood, productOptions]);
  const selectedTargetFood = useMemo(() => {
    return productOptions.find((option) => option.label === targetFood || option.product_key === targetFood);
  }, [targetFood, productOptions]);
  const keyProfileDiff = useMemo(() => {
    return (compareResult?.profile_diff || []).filter((row) => {
      if (row.diff_b_minus_a === null || row.diff_b_minus_a === undefined) return false;
      return Math.abs(row.diff_b_minus_a) >= 15;
    });
  }, [compareResult]);

  return (
    <div className="min-h-screen bg-[#f5f8ff] px-5 py-6 text-slate-900">
      <div className="mx-auto max-w-[1840px] space-y-4">
        <header className="flex flex-col justify-between gap-4 md:flex-row md:items-start">
          <div>
            <h1 className="text-[28px] font-bold tracking-tight text-slate-950">猫粮对比分析</h1>
            <p className="mt-2 text-sm font-medium text-slate-500">
              分析当前粮和对比粮的配方差异，帮你找到更适合猫咪的主粮
            </p>
          </div>
          <button
            type="button"
            onClick={() => setMissingProductModalOpen(true)}
            className="inline-flex items-center justify-center gap-2 rounded-2xl border border-slate-200 bg-white px-5 py-2.5 text-sm font-semibold text-blue-600 shadow-sm hover:border-blue-200 hover:bg-blue-50"
          >
            <span className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-blue-500 text-[10px]">i</span>
            不知道粮名？ 上传配料表图片分析
          </button>
        </header>

        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="grid gap-5 lg:grid-cols-[0.9fr_1fr_1.1fr]">
            <div>
              <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-950">
                <span className="text-blue-600">♣</span>
                <span>猫龄</span>
              </div>
              <select
                value={catProfile.age}
                onChange={(event) => setCatProfile((prev) => ({ ...prev, age: event.target.value }))}
                className="h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm font-medium text-slate-950 outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
              >
                {CAT_AGE_OPTIONS.map((age) => (
                  <option key={age} value={age}>
                    {age}
                  </option>
                ))}
              </select>
            </div>

            <ProductPicker
              label="当前粮"
              value={currentFood}
              options={productOptions}
              loading={productsLoading}
              onChange={handleCurrentFoodChange}
            />

            <ProductPicker
              label="对比粮（可选）"
              value={targetFood}
              options={productOptions}
              loading={productsLoading}
              onChange={handleTargetFoodChange}
            />
          </div>

          {productsLoading && <div className="mt-4 text-sm text-slate-500">正在加载产品库...</div>}
          {productsError && (
            <div className="mt-4 rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{productsError}</div>
          )}

          {missingProductSubmission && (
            <div className="mt-3 flex flex-wrap items-center gap-2 rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-600">
              <span className="rounded-lg bg-white px-3 py-1">品牌：{missingProductSubmission.brandName}</span>
              <span className="rounded-lg bg-white px-3 py-1">产品：{missingProductSubmission.productName}</span>
              <span className="rounded-lg bg-white px-3 py-1">图片：{missingProductSubmission.fileName}</span>
              <button type="button" onClick={removeMissingProductSubmission} className="text-xs text-slate-500 underline">移除</button>
            </div>
          )}

          <div className="mt-5 grid gap-4 border-t border-slate-100 pt-4 xl:grid-cols-[1fr_auto] xl:items-center">
            <div className="space-y-3">
              <InlineMultiSelect
                label="历史问题"
                note="可多选"
                options={HISTORY_ISSUE_OPTIONS}
                selected={catProfile.historyIssues}
                onChange={(historyIssues) => setCatProfile((prev) => ({ ...prev, historyIssues }))}
              />
              <InlineMultiSelect
                label="近期症状"
                note="可多选"
                options={RECENT_SYMPTOM_OPTIONS}
                selected={catProfile.recentSymptoms}
                onChange={(recentSymptoms) => setCatProfile((prev) => ({ ...prev, recentSymptoms }))}
              />
            </div>
            <button
              type="button"
              disabled={!canCompare}
              onClick={handleCompare}
              className="h-12 shrink-0 rounded-xl bg-blue-600 px-9 text-sm font-bold text-white shadow-lg shadow-blue-600/25 hover:bg-blue-500 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:shadow-none"
            >
              {compareLoading ? "正在分析..." : "开始分析 →"}
            </button>
          </div>

          {compareError && (
            <div className="mt-4 rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{compareError}</div>
          )}
        </section>

        {compareResult && (
          <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center">
              <h2 className="text-lg font-bold text-slate-950">分析结果</h2>
              <button
                type="button"
                onClick={() => setMissingProductModalOpen(true)}
                className="text-left text-sm font-medium text-blue-600 hover:text-blue-500"
              >
                这两款都不合适？试试智能推荐 →
              </button>
            </div>

            <section className="mt-4">
              <SectionTitle marker="A" title="原材料展示" hint="查看两款粮的基础信息和原始配料内容" />
              <div className="grid gap-4 lg:grid-cols-2">
                <ProductIngredientCard label="当前粮" product={compareResult.current_food} option={selectedCurrentFood} />
                <ProductIngredientCard label="对比粮" product={compareResult.target_food} option={selectedTargetFood} />
              </div>
            </section>

            <section className="mt-4 grid gap-4 lg:grid-cols-2">
              <div className="lg:col-span-2">
                <SectionTitle marker="B" title="两款粮侧重点" hint="按蛋白、碳水、脂肪、纤维等配方维度对比分数" />
              </div>
              <FocusBarComparison
                currentProfile={compareResult.current_food.profile}
                targetProfile={compareResult.target_food.profile}
                currentName={productDisplayName(compareResult.current_food)}
                targetName={productDisplayName(compareResult.target_food)}
              />
              <KeyChangeSummary
                rows={keyProfileDiff}
                currentName={productDisplayName(compareResult.current_food)}
                targetName={productDisplayName(compareResult.target_food)}
              />
            </section>

            <section className="mt-4">
              <SectionTitle marker="C" title="潜在风险对比" hint="基于产品库横向位置，识别两款粮在常见风险维度上的差异" />
              <DimensionComparisonPanel
                rows={compareResult.friendly_rows}
                currentName={productDisplayName(compareResult.current_food)}
                targetName={productDisplayName(compareResult.target_food)}
              />
            </section>

            <section className="mt-4">
              <div className="flex flex-col justify-between gap-3 md:flex-row md:items-start">
                <SectionTitle marker="D" title="大模型总结" hint="综合配方差异、潜在风险和猫咪情况生成建议" />
                <button
                  type="button"
                  disabled={summaryLoading}
                  onClick={handleGenerateSummary}
                  className="h-10 shrink-0 rounded-xl bg-blue-600 px-5 text-sm font-bold text-white shadow-lg shadow-blue-600/20 hover:bg-blue-500 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:shadow-none"
                >
                  {summaryLoading ? "正在生成..." : "生成大模型总结"}
                </button>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-white p-5">

                {summaryError && (
                  <div className="mt-4 rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{summaryError}</div>
                )}

                {summary ? (
                  <div className="mt-5 whitespace-pre-wrap rounded-3xl border border-slate-200 bg-white p-6 text-sm leading-7 text-slate-700 shadow-sm">
                    {summary}
                  </div>
                ) : (
                  <div className="mt-5 rounded-2xl bg-slate-50 p-5 text-sm leading-6 text-slate-600">
                    点击“生成大模型总结”后，大模型会围绕两款粮各有优势、整体产品池表现、换到对比粮可能改善什么和怎么选进行总结。
                  </div>
                )}
              </div>
            </section>
          </section>
        )}
      </div>
      <MissingProductUploadModal
        open={missingProductModalOpen}
        brandOptions={brandOptions}
        onClose={() => setMissingProductModalOpen(false)}
        onSubmit={(submission) => {
          removeMissingProductSubmission();
          void handleMissingProductSubmit(submission);
        }}
      />
    </div>
  );
}
