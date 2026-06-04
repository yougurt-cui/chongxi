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

function ProductIngredientCard(props: {
  label: string;
  product: ProductInfo;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="text-sm font-semibold text-slate-600">{props.label}</div>
      <div className="mt-1 text-xl font-bold leading-snug text-slate-950">{props.product.name}</div>
      {props.product.brand_name && (
        <div className="mt-1 text-sm text-slate-500">品牌：{props.product.brand_name}</div>
      )}
      <div className="mt-4 max-h-40 overflow-auto rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm leading-6 text-slate-600">
        <div className="mb-1 font-semibold text-slate-700">原始配料信息</div>
        {props.product.ingredient_composition || "暂无原始配料信息"}
      </div>
    </div>
  );
}

export default function CatFoodComparePage() {
  const [products, setProducts] = useState<string[]>([]);
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
          fetch("/api/cat-food-compare/products"),
          fetch("/api/cat-food-compare/brands"),
        ]);
        const data = await productsResponse.json();
        if (!productsResponse.ok) throw new Error(data.error || "产品库加载失败");
        const brandsData = await brandsResponse.json();
        if (!brandsResponse.ok) throw new Error(brandsData.error || "品牌库加载失败");
        if (cancelled) return;
        const nextProducts = data.products || [];
        setProducts(nextProducts);
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

  const selectedDescription = useMemo(() => {
    if (!currentFood || !targetFood) return "请选择当前粮和对比粮";
    if (currentFood === targetFood) return "当前粮和对比粮不能相同";
    return `当前粮：${currentFood} ｜ 对比粮：${targetFood}`;
  }, [currentFood, targetFood]);
  const keyProfileDiff = useMemo(() => {
    return (compareResult?.profile_diff || []).filter((row) => {
      if (row.diff_b_minus_a === null || row.diff_b_minus_a === undefined) return false;
      return Math.abs(row.diff_b_minus_a) >= 15;
    });
  }, [compareResult]);

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-8 text-slate-900">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="rounded-3xl bg-white p-6 shadow-sm">
          <div className="mb-2 text-sm font-medium text-slate-500">宠析 · C端 Demo</div>
          <h1 className="text-2xl font-bold tracking-tight md:text-3xl">猫粮对比分析</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
            选择当前粮和对比粮，系统会基于猫粮库中的品牌、产品名、配方画像和风险结果，生成清晰的换粮判断。
          </p>
        </header>

        <section className="rounded-3xl bg-white p-6 shadow-sm">
          <div className="flex flex-col justify-between gap-3 md:flex-row md:items-start">
            <div>
              <h2 className="text-lg font-semibold">输入信息</h2>
              <p className="mt-1 text-sm text-slate-500">
                当前粮和对比粮均来自猫粮库，格式为品牌 + 产品名。
              </p>
            </div>
            {productsLoading && <span className="text-sm text-slate-500">正在加载产品库...</span>}
          </div>

          {productsError && (
            <div className="mt-4 rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{productsError}</div>
          )}

          <div className="mt-5 grid gap-4 md:grid-cols-3">
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-700">猫龄</label>
              <select
                value={catProfile.age}
                onChange={(event) => setCatProfile((prev) => ({ ...prev, age: event.target.value }))}
                className="h-[42px] w-full rounded-xl border border-slate-200 bg-white px-3 text-sm outline-none focus:border-slate-900"
              >
                {CAT_AGE_OPTIONS.map((age) => (
                  <option key={age} value={age}>
                    {age}
                  </option>
                ))}
              </select>
            </div>

            <ProductCombobox
              label="当前粮"
              value={currentFood}
              options={products}
              placeholder="输入品牌或产品名搜索"
              onChange={setCurrentFood}
            />

            <ProductCombobox
              label="对比粮"
              value={targetFood}
              options={products}
              placeholder="输入品牌或产品名搜索"
              onChange={setTargetFood}
            />
          </div>

          <MissingProductUploadEntry
            submitted={missingProductSubmission}
            onOpen={() => setMissingProductModalOpen(true)}
            onRemove={removeMissingProductSubmission}
          />

          <div className="mt-5 grid gap-5 md:grid-cols-2">
            <MultiSelect
              label="历史问题"
              description="选择猫咪过去出现过的长期或反复问题。"
              options={HISTORY_ISSUE_OPTIONS}
              selected={catProfile.historyIssues}
              onChange={(historyIssues) => setCatProfile((prev) => ({ ...prev, historyIssues }))}
            />
            <MultiSelect
              label="近期症状"
              description="选择最近换粮前后需要重点观察的表现。"
              options={RECENT_SYMPTOM_OPTIONS}
              selected={catProfile.recentSymptoms}
              onChange={(recentSymptoms) => setCatProfile((prev) => ({ ...prev, recentSymptoms }))}
            />
          </div>

          <div className="mt-5 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">{selectedDescription}</div>

          <div className="mt-5 flex justify-end">
            <button
              type="button"
              disabled={!canCompare}
              onClick={handleCompare}
              className="rounded-full bg-slate-900 px-5 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {compareLoading ? "正在对比..." : "开始对比"}
            </button>
          </div>

          {compareError && (
            <div className="mt-4 rounded-2xl bg-rose-50 p-4 text-sm text-rose-700">{compareError}</div>
          )}
        </section>

        {compareResult && (
          <>
            <section className="grid gap-4 lg:grid-cols-2">
              <ProductIngredientCard label="当前粮" product={compareResult.current_food} />
              <ProductIngredientCard label="对比粮" product={compareResult.target_food} />
            </section>

            <section className="rounded-3xl bg-white p-6 shadow-sm">
              <div className="flex flex-col justify-between gap-3 md:flex-row md:items-start">
                <div>
                  <h2 className="text-lg font-semibold">两款粮侧重点</h2>
                  <p className="mt-1 text-sm text-slate-500">
                    只展示差值达到 15 分以上的关键变化维度：15–30 为明显变化，超过 30 为重点变化。
                  </p>
                </div>
              </div>

              <div className="mt-5 grid gap-4 lg:grid-cols-2">
                <RadarChart
                  title={`当前粮｜${productDisplayName(compareResult.current_food)}`}
                  profile={compareResult.current_food.profile}
                  baseline={compareResult.current_food.baseline_profile}
                  color="#0f172a"
                />
                <RadarChart
                  title={`对比粮｜${productDisplayName(compareResult.target_food)}`}
                  profile={compareResult.target_food.profile}
                  baseline={compareResult.target_food.baseline_profile}
                  color="#2563eb"
                />
              </div>

              <div className="mt-5 overflow-auto rounded-2xl border border-slate-200">
                <table className="w-full min-w-[860px] border-collapse text-left text-sm">
                  <thead className="bg-slate-100 text-slate-600">
                    <tr>
                      <th className="px-4 py-3 font-medium">关键变化维度</th>
                      <th className="px-4 py-3 font-medium">
                        当前粮｜{productDisplayName(compareResult.current_food)}
                      </th>
                      <th className="px-4 py-3 font-medium">
                        对比粮｜{productDisplayName(compareResult.target_food)}
                      </th>
                      <th className="px-4 py-3 font-medium">差值</th>
                      <th className="px-4 py-3 font-medium">变化级别</th>
                      <th className="px-4 py-3 font-medium">变化提示</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keyProfileDiff.map((row) => (
                      <tr key={row.dimension} className="border-t border-slate-100">
                        <td className="px-4 py-4 font-medium text-slate-900">{row.dimension}</td>
                        <td className="px-4 py-4">
                          <div>{formatScore(row.product_a_score)}</div>
                        </td>
                        <td className="px-4 py-4">
                          <div>{formatScore(row.product_b_score)}</div>
                        </td>
                        <td className="px-4 py-4 font-medium text-slate-900">
                          {row.diff_b_minus_a === null || row.diff_b_minus_a === undefined
                            ? "暂无"
                            : Math.abs(row.diff_b_minus_a).toFixed(1)}
                        </td>
                        <td className="px-4 py-4">
                          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-700">
                            {getRowDiffLevel(row)}
                          </span>
                        </td>
                        <td className="px-4 py-4 leading-6 text-slate-600">
                          {diffTip(
                            row,
                            productDisplayName(compareResult.current_food),
                            productDisplayName(compareResult.target_food),
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!keyProfileDiff.length && (
                <div className="mt-4 rounded-2xl bg-slate-50 p-4 text-sm text-slate-500">
                  两款粮暂无差值达到 15 分以上的关键变化维度。
                </div>
              )}
            </section>

            <section className="rounded-3xl bg-white p-6 shadow-sm">
              <h2 className="text-lg font-semibold">两款粮在所有粮中表现</h2>
              <p className="mt-1 text-sm text-slate-500">
                友好度来自产品库中的风险结果换算，用于横向比较当前粮和对比粮在整体产品池中的相对位置。
              </p>

              <div className="mt-5 overflow-auto rounded-2xl border border-slate-200">
                <table className="w-full min-w-[860px] border-collapse text-left text-sm">
                  <thead className="bg-slate-100 text-slate-600">
                    <tr>
                      <th className="px-4 py-3 font-medium">友好度维度</th>
                      <th className="px-4 py-3 font-medium">
                        当前粮｜{productDisplayName(compareResult.current_food)}
                      </th>
                      <th className="px-4 py-3 font-medium">
                        对比粮｜{productDisplayName(compareResult.target_food)}
                      </th>
                      <th className="px-4 py-3 font-medium">横向解读</th>
                    </tr>
                  </thead>
                  <tbody>
                    {compareResult.friendly_rows.map((row) => (
                      <tr key={row.dimension} className="border-t border-slate-100">
                        <td className="px-4 py-4 font-medium text-slate-900">{row.dimension}</td>
                        <td className="px-4 py-4">
                          <span className={`rounded-full px-2.5 py-1 text-xs ${levelTone(row.current)}`}>
                            {row.current}
                          </span>
                        </td>
                        <td className="px-4 py-4">
                          <span className={`rounded-full px-2.5 py-1 text-xs ${levelTone(row.target)}`}>
                            {row.target}
                          </span>
                        </td>
                        <td className="px-4 py-4 leading-6 text-slate-600">{row.interpretation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="rounded-3xl bg-white p-6 shadow-sm">
              <div className="flex flex-col justify-between gap-3 md:flex-row md:items-start">
                <div>
                  <h2 className="text-lg font-semibold">模块三：大模型总结</h2>
                </div>
                <button
                  type="button"
                  disabled={summaryLoading}
                  onClick={handleGenerateSummary}
                  className="rounded-full bg-slate-900 px-5 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {summaryLoading ? "正在生成..." : "生成大模型总结"}
                </button>
              </div>

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
            </section>
          </>
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
