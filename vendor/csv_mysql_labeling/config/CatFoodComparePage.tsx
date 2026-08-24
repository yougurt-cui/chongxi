import React, { useEffect, useMemo, useState } from "react";

type Product = {
  value?: string;
  option?: ProductOption;
  brand: string;
  name: string;
  price: string;
  origin: string;
  selected?: boolean;
  packageTone: string;
  imageUrl?: string | null;
  ingredients: string;
  materialRoleEvidence?: MaterialRoleEvidence;
  scores: Array<{ label: string; value: number }>;
  risks: Array<{ label: string; value: number }>;
  nutritionSummary: string;
  riskSummary: string;
};

type CatProfile = {
  age: string;
  historyIssues: string[];
  recentSymptoms: string[];
};

type ProfilePoint = {
  dimension: string;
  score: number | null;
};

type FriendlyRow = {
  dimension: string;
  current_score: number | null;
  target_score: number | null;
};

type ProductInfo = {
  query: string;
  formula_id?: string | number | null;
  source_id?: string | number | null;
  product_key?: string | null;
  name: string;
  brand_name: string;
  ingredient_composition: string;
  material_role_evidence?: MaterialRoleEvidence;
  profile: ProfilePoint[];
};

type MaterialRoleEvidence = {
  raw_ingredient_text?: unknown;
  ingredient_items?: IngredientRoleItem[];
  protein_roles?: Record<string, unknown>;
  protein_score_rules?: Record<string, unknown>;
  fat_roles?: Record<string, unknown>;
  fiber_carb_roles?: Record<string, unknown>;
};

type IngredientRoleItem = {
  position?: number | string | null;
  raw_name?: string | null;
  standard_name?: string | null;
  ingredient_family?: string | null;
  primary_nutrition_role?: string | null;
  is_protein?: boolean | number | null;
  is_plant_protein?: boolean | number | null;
  features_json?: unknown;
};

type ProductOption = {
  id: string;
  catalog_key: string;
  formula_id?: string | number | null;
  product_key?: string | null;
  label: string;
  brand: string;
  raw_brand?: string | null;
  product_name: string;
  raw_title?: string | null;
  origin_type?: string | null;
  price_bucket?: string | null;
  price_band?: string | null;
  main_image_url?: string | null;
  main_images?: string[];
  compare_available: boolean;
  score_source_id?: string | number | null;
  display_text?: string | null;
};

type CompareResult = {
  current_food: ProductInfo;
  target_food: ProductInfo;
  friendly_rows: FriendlyRow[];
  llm_context: unknown;
};

type AdviceTone = "positive" | "watch" | "caution" | "danger";

type ChangeFoodAdvice = {
  scenarios: Array<{
    id: string;
    tone: AdviceTone;
    title: string;
    recommendationLabel: string;
    subtitle: string;
    matchConditions: string[];
    reasonSummary: string;
    reasonEvidence: Array<{ label: string; currentValue: number; targetValue: number }>;
    caution: string;
    extraReason?: string | null;
    warningSignal?: string | null;
  }>;
  transitionPlan: Array<{
    period: string;
    newFoodPercent: number;
    oldFoodPercent: number;
  }>;
  disclaimer: string;
};

type CatFoodTask = {
  id: string;
  status: "pending" | "running" | "success" | "failed";
  result?: {
    compare?: CompareResult;
  };
  error_message?: string;
};

function priceLabel(option?: ProductOption) {
  if (!option) return "";
  if (option.price_band && option.price_band !== "未知") return option.price_band;
  if (option.price_bucket && option.price_bucket !== "未知") return option.price_bucket.includes("￥") ? option.price_bucket : option.price_bucket;
  return "";
}

function productOptionValue(option: ProductOption) {
  return option.product_key || option.catalog_key || option.label;
}

function findProductOption(options: ProductOption[], value: string) {
  return options.find((option) => {
    const stableValue = productOptionValue(option);
    return stableValue === value || option.product_key === value || option.catalog_key === value || option.label === value;
  });
}

function profileScore(profile: ProfilePoint[] | undefined, dimension: string) {
  const point = (profile || []).find((item) => item.dimension === dimension);
  return point?.score === null || point?.score === undefined ? null : Number(point.score);
}

function riskScore(rows: FriendlyRow[] | undefined, label: string, side: "current" | "target") {
  const row = (rows || []).find((item) => item.dimension.includes(label));
  const value = side === "current" ? row?.current_score : row?.target_score;
  return value === null || value === undefined ? null : Number(value);
}

function toneForIndex(index: number) {
  return [
    "from-sky-200 via-white to-blue-500",
    "from-slate-100 via-white to-red-100",
    "from-blue-100 via-white to-slate-300",
    "from-slate-200 via-blue-100 to-slate-900",
    "from-amber-100 via-white to-stone-400",
  ][index % 5];
}

const products: Product[] = [
  {
    brand: "Ziwi Peak 巅峰",
    name: "风干牛肉犬粮",
    price: "￥300 - ￥600",
    origin: "进口",
    packageTone: "from-sky-200 via-white to-blue-500",
    ingredients: "牛肉、牛内脏、牛心、牛骨、牛肝、牛血、海带、苹果、西瓜、亚麻籽、新西兰绿贻贝、菊苣纤维、海藻、维生素、矿物质等。",
    scores: [
      { label: "蛋白质量", value: 93 },
      { label: "蛋白配方", value: 88 },
      { label: "碳水负担", value: 28 },
      { label: "脂肪负担", value: 72 },
      { label: "纤维维护", value: 70 },
      { label: "菌群友好", value: 62 },
      { label: "皮肤保护", value: 79 },
    ],
    risks: [
      { label: "黑下巴友好度", value: 48.6 },
      { label: "软便友好度", value: 48.6 },
    ],
    nutritionSummary: "巅峰在蛋白质量、菌群支持与皮肤保护等维度表现优秀，整体营养强度高，适合体质良好、活力充沛的狗狗。",
    riskSummary: "黑下巴风险与软便风险均为中等，处于可接受范围，适合作为长期主粮，但需留意皮肤体质。",
  },
  {
    brand: "Royal Canin 皇家",
    name: "中型犬成犬粮",
    price: "￥200 - ￥400",
    origin: "进口/国产",
    packageTone: "from-slate-100 via-white to-red-100",
    ingredients: "脱水禽肉、玉米、小麦、小麦粉、玉米粉、禽油、玉米蛋白粉、动物油脂、甜菜粕、鱼油、植物蛋白、矿物质、维生素、益生元等。",
    scores: [
      { label: "蛋白质量", value: 78 },
      { label: "蛋白配方", value: 70 },
      { label: "碳水负担", value: 62 },
      { label: "脂肪负担", value: 58 },
      { label: "纤维维护", value: 46 },
      { label: "菌群友好", value: 55 },
      { label: "皮肤保护", value: 60 },
    ],
    risks: [
      { label: "黑下巴友好度", value: 48.6 },
      { label: "软便友好度", value: 48.6 },
    ],
    nutritionSummary: "皇家营养较为均衡，各项指标处于中等水平，能满足中型犬日常营养需求。",
    riskSummary: "黑下巴风险与软便风险均为中等，整体风险可控，适合大多数健康狗狗。",
  },
  {
    brand: "伯纳天纯",
    name: "低敏鸭肉狗粮",
    price: "￥180 - ￥350",
    origin: "国产",
    packageTone: "from-blue-100 via-white to-slate-300",
    ingredients: "鸭肉、鸭肉粉、糙米、燕麦、鱼油、甜菜粕、酵母水解物、亚麻籽、益生元、维生素与矿物质。",
    scores: [],
    risks: [],
    nutritionSummary: "",
    riskSummary: "",
  },
  {
    brand: "Orijen 渴望",
    name: "六种鱼全犬粮",
    price: "￥400 - ￥750",
    origin: "进口",
    packageTone: "from-slate-200 via-blue-100 to-slate-900",
    ingredients: "新鲜鱼、鱼粉、豆类、鱼油、南瓜、苹果、梨、蔓越莓、蓝莓、海藻、维生素与矿物质。",
    scores: [],
    risks: [],
    nutritionSummary: "",
    riskSummary: "",
  },
  {
    brand: "ACANA 爱肯拿",
    name: "幼犬成犬犬粮",
    price: "￥220 - ￥420",
    origin: "进口",
    packageTone: "from-amber-100 via-white to-stone-400",
    ingredients: "鸡肉、鱼肉、豆类、南瓜、苹果、梨、蔓越莓、草本植物、维生素与矿物质。",
    scores: [],
    risks: [],
    nutritionSummary: "",
    riskSummary: "",
  },
];

const scoreDimensions = ["蛋白质量", "蛋白压力", "碳水负担", "脂肪负担", "纤维缓冲", "菌群支持", "皮肤保护"];
const ORIJEN_PRODUCT_IMAGE = "/products/orijen-wild-reserve-kitten.png";
const PRODUCT_CAROUSEL_PAGE_SIZE = 5;

function productImageUrl(product: Product): string | null {
  const identity = `${product.brand} ${product.name}`.toLowerCase();
  if (identity.includes("渴望") || identity.includes("orijen")) {
    return ORIJEN_PRODUCT_IMAGE;
  }
  return product.imageUrl || null;
}

function optionToProduct(option: ProductOption, index: number, selected: boolean): Product {
  return {
    value: productOptionValue(option),
    option,
    brand: option.brand || "待确认",
    name: option.product_name || option.label,
    price: priceLabel(option) || "暂无",
    origin: option.origin_type?.includes("进口") ? "进口" : option.origin_type?.includes("国产") ? "国产" : "待确认",
    selected,
    imageUrl: option.main_image_url || option.main_images?.[0] || null,
    packageTone: toneForIndex(index),
    ingredients: option.display_text || option.raw_title || "暂无原料信息",
    materialRoleEvidence: undefined,
    scores: [],
    risks: [],
    nutritionSummary: "",
    riskSummary: "",
  };
}

function resultToProduct(
  info: ProductInfo,
  option: ProductOption | undefined,
  rows: FriendlyRow[],
  side: "current" | "target",
  index: number,
): Product {
  const blackChin = riskScore(rows, "黑下巴", side);
  const softStool = riskScore(rows, "软便", side) ?? riskScore(rows, "肠胃", side);
  return {
    value: option ? productOptionValue(option) : info.product_key || info.query,
    option,
    brand: info.brand_name || option?.brand || "待确认",
    name: info.name || option?.product_name || info.query,
    price: priceLabel(option) || "暂无",
    origin: option?.origin_type?.includes("进口") ? "进口" : option?.origin_type?.includes("国产") ? "国产" : "待确认",
    selected: true,
    imageUrl: option?.main_image_url || option?.main_images?.[0] || null,
    packageTone: toneForIndex(index),
    ingredients: info.ingredient_composition || option?.display_text || "暂无原料信息",
    materialRoleEvidence: info.material_role_evidence,
    scores: scoreDimensions.map((label) => ({ label, value: profileScore(info.profile, label) ?? 0 })),
    risks: [
      { label: "黑下巴友好度", value: blackChin ?? 0 },
      { label: "软便友好度", value: softStool ?? 0 },
    ],
    nutritionSummary: `${info.brand_name || option?.brand || "该产品"}的营养指标已完成结构化评估，可结合上方得分和原料内容判断是否适合当前宠物。`,
    riskSummary: "黑下巴风险与软便风险均为全库相对倾向，建议结合宠物实际反馈和换粮观察期判断。",
  };
}

function PackageImage({ product, large = false }: { product: Product; large?: boolean }) {
  const imageUrl = productImageUrl(product);
  if (imageUrl) {
    return <img src={imageUrl} alt={`${product.brand} ${product.name}`} className="h-full w-full rounded-xl object-contain p-2" />;
  }
  return (
    <div className={`relative h-full w-full overflow-hidden rounded-xl bg-gradient-to-br ${product.packageTone}`}>
      <div className="absolute inset-x-7 bottom-5 h-6 rounded-full bg-amber-900/20 blur-md" />
      <div className={`absolute left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center rounded-t-[28px] rounded-b-xl border border-white/80 bg-white/80 shadow-xl ${large ? "h-[132px] w-[92px] p-3" : "h-[118px] w-[78px] p-2"}`}>
        <div className="h-3 w-12 rounded-full bg-indigo-200" />
        <div className="mt-8 text-center text-[12px] font-semibold leading-4 text-indigo-700">{product.brand.split(" ")[0]}</div>
        <div className="mt-auto h-9 w-16 rounded-[50%] bg-amber-700" />
      </div>
    </div>
  );
}

function HeaderNav() {
  const navItems = ["宠物瞬间", "宠物助手", "宠物周边", "我的宠物"];
  return (
    <header className="fixed inset-x-0 top-0 z-50 h-[72px] border-b border-[#D9E0EE] bg-white shadow-[0_3px_14px_rgba(30,41,59,0.06)]">
      <div className="grid h-full grid-cols-[280px_minmax(0,1fr)_180px] items-center px-10 2xl:grid-cols-[280px_minmax(0,1fr)_200px]">
        <div className="text-[32px] font-extrabold leading-none text-[#3F35FF]">宠析</div>
        <nav className="flex h-full justify-center gap-16 text-[16px] font-semibold text-[#172033]">
          {navItems.map((item) => {
            const active = item === "宠物助手";
            return (
            <a key={item} href="#" className={`relative flex h-full items-center ${active ? "text-[#3F35FF]" : ""}`}>
              {item}
              {active && <span className="absolute bottom-0 left-1/2 h-1 w-18 -translate-x-1/2 rounded-t-full bg-[#3F35FF]" />}
            </a>
            );
          })}
        </nav>
        <div aria-hidden="true" />
      </div>
    </header>
  );
}

function PetInfoIcon({ type }: { type: "breed" | "age" | "food" | "issue" }) {
  const commonProps = {
    className: "h-4 w-4",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (type === "breed") {
    return (
      <svg {...commonProps}>
        <circle cx="7.5" cy="8" r="1.7" />
        <circle cx="12" cy="6" r="1.7" />
        <circle cx="16.5" cy="8" r="1.7" />
        <circle cx="18.5" cy="12" r="1.5" />
        <path d="M8.1 13.1c1.1-1.8 2.3-2.7 3.9-2.7s2.8.9 3.9 2.7c1.4 2.4-.1 4.9-2.6 4.1a4.2 4.2 0 0 0-2.6 0c-2.5.8-4-1.7-2.6-4.1Z" />
      </svg>
    );
  }
  if (type === "age") {
    return (
      <svg {...commonProps}>
        <rect x="4" y="5.5" width="16" height="14" rx="2.5" />
        <path d="M8 3.5v4M16 3.5v4M4 10h16" />
        <path d="M8 13h3M8 16h5" />
      </svg>
    );
  }
  if (type === "food") {
    return (
      <svg {...commonProps}>
        <path d="M5 10.5h14l-1.3 6.2a2 2 0 0 1-2 1.6H8.3a2 2 0 0 1-2-1.6L5 10.5Z" />
        <path d="M4 10.5h16M8 7.5c1.1-1.2 2.3-1.8 4-1.8s2.9.6 4 1.8" />
      </svg>
    );
  }
  return (
    <svg {...commonProps}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5v5.5M12 16.5h.01" />
    </svg>
  );
}

function FavoriteIcon({ selected = false, className = "h-5 w-5" }: { selected?: boolean; className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill={selected ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z" />
    </svg>
  );
}

function ChevronDownIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m7 10 5 5 5-5" />
    </svg>
  );
}

function EmptyCompareIcon() {
  return (
    <svg className="h-12 w-12" viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <rect x="7" y="9" width="24" height="29" rx="5" fill="white" stroke="currentColor" strokeWidth="2" />
      <rect x="12" y="14" width="14" height="11" rx="2" fill="currentColor" opacity=".14" />
      <path d="M12 30h14M12 34h9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" opacity=".65" />
      <path d="M38.7 20.2a5.1 5.1 0 0 0-7.2 0l-.7.8-.8-.8a5.1 5.1 0 0 0-7.2 7.2l.8.8 7.2 6.9 7.1-6.9.8-.8a5.1 5.1 0 0 0 0-7.2Z" fill="white" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
}

function SidebarToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 6h16M4 12h16M4 18h16" />
      <path d={collapsed ? "m14 9 3 3-3 3" : "m10 9-3 3 3 3"} />
    </svg>
  );
}

function PetSidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const petRows = [
    ["breed", "品种", "布偶"],
    ["age", "年龄", "3岁"],
    ["food", "主食", "皇家"],
    ["issue", "问题", "无"],
  ] as const;
  return (
    <aside className={`scrollbar-none fixed bottom-0 left-0 top-[72px] z-40 overflow-y-auto ${collapsed ? "w-[72px] px-3" : "w-[280px] px-5 max-[1599px]:w-[260px] max-[1365px]:w-[240px]"} border-r border-[#D9E0EE] bg-white py-8 shadow-[4px_0_14px_rgba(30,41,59,0.04)] transition-[width,padding] duration-300`}>
      <button
        type="button"
        onClick={onToggle}
        title={collapsed ? "展开侧边栏" : "收起侧边栏"}
        aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
        className={`mb-6 flex h-10 items-center rounded-xl text-[14px] font-medium text-[#4B5563] transition hover:bg-[#F3F5FF] hover:text-[#3F35FF] ${collapsed ? "w-full justify-center" : "w-full gap-3 px-3"}`}
      >
        <SidebarToggleIcon collapsed={collapsed} />
        {!collapsed && <span>收起侧边栏</span>}
      </button>

      {!collapsed && <section>
        <h2 className="mb-5 flex items-center gap-3 text-[18px] font-bold text-[#172033]"><span className="text-[#3F35FF]">▧</span>宠物档案</h2>
        <div className="rounded-2xl border border-[#E5E9F5] bg-white p-5 shadow-[0_8px_24px_rgba(30,41,59,0.06)]">
          <div className="space-y-5">
            {petRows.map(([icon, label, value]) => (
              <div key={label} className="flex items-center justify-between gap-4 text-[14px]">
                <span className="flex items-center gap-3 font-medium text-[#4B5563]">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#F3F5FF] text-[#3F35FF]">
                    <PetInfoIcon type={icon} />
                  </span>
                  {label}
                </span>
                <span className="max-w-[112px] truncate text-right font-semibold text-[#172033]" title={value}>{value}</span>
              </div>
            ))}
          </div>
        </div>
      </section>}

      {!collapsed && <section className="mt-8">
        <h2 className="mb-5 flex items-center gap-3 text-[18px] font-bold text-[#172033]"><span className="text-[#3F35FF]">☏</span>联系方式</h2>
        <div className="space-y-4 rounded-2xl border border-[#E5E9F5] bg-white p-5 text-[13px] font-medium text-[#4B5563] shadow-[0_8px_24px_rgba(30,41,59,0.06)]">
          <div className="flex items-center gap-3"><span className="text-[#94A3B8]">⌕</span>400-888-1234</div>
          <div className="flex items-center gap-3"><span className="text-[#94A3B8]">✉</span>service@chongxi.com</div>
          <div className="flex items-center gap-3"><span className="text-[#94A3B8]">◴</span>工作日 9:00 - 18:00</div>
        </div>
      </section>}

      {!collapsed && <section className="mt-8">
        <h2 className="mb-5 flex items-center gap-3 text-[18px] font-bold text-[#172033]"><span className="text-[#3F35FF]">▣</span>留言板</h2>
        <div className="flex h-[220px] flex-col items-center justify-center rounded-2xl border border-[#E5E9F5] bg-white text-center text-[13px] font-normal leading-6 text-[#7A8499] shadow-[0_8px_24px_rgba(30,41,59,0.06)]">
          <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-100 text-2xl text-slate-300">✎</div>
          在这里记录宠物的饮食反馈、<br />身体状况或其他备注...
        </div>
      </section>}
    </aside>
  );
}

function SearchFilterBar(props: {
  brandQuery: string;
  productQuery: string;
  priceFilter: string;
  originFilter: string;
  onBrandQueryChange: (value: string) => void;
  onProductQueryChange: (value: string) => void;
  onPriceFilterChange: (value: string) => void;
  onOriginFilterChange: (value: string) => void;
}) {
  return (
    <section className="min-h-[56px] rounded-xl border border-[#E5E9F5] bg-white px-5 py-2 shadow-[0_8px_24px_rgba(30,41,59,0.04)]">
      <div className="grid h-full grid-cols-[1fr_1.15fr_1.1fr_0.9fr_auto] items-center gap-5 max-[1399px]:grid-cols-2">
        <label className="grid grid-cols-[64px_1fr] items-center gap-2 text-[14px] font-semibold text-[#172033]">
          品牌名
          <input value={props.brandQuery} onChange={(event) => props.onBrandQueryChange(event.target.value)} placeholder="请输入品牌名" className="h-9 min-w-0 rounded-lg border border-[#E5E9F5] bg-white px-4 text-[14px] font-normal text-[#172033] outline-none placeholder:font-normal placeholder:text-[#A0A8BA]" />
        </label>
        <label className="grid grid-cols-[64px_1fr] items-center gap-2 text-[14px] font-semibold text-[#172033]">
          产品名
          <input value={props.productQuery} onChange={(event) => props.onProductQueryChange(event.target.value)} placeholder="请输入产品名" className="h-9 min-w-0 rounded-lg border border-[#E5E9F5] bg-white px-4 text-[14px] font-normal text-[#172033] outline-none placeholder:font-normal placeholder:text-[#A0A8BA]" />
        </label>
        <label className="grid grid-cols-[64px_1fr] items-center gap-2 text-[14px] font-semibold text-[#172033]">
          价格带
          <select value={props.priceFilter} onChange={(event) => props.onPriceFilterChange(event.target.value)} className="h-9 min-w-0 rounded-lg border border-[#E5E9F5] bg-white px-4 text-[14px] font-normal text-[#7A8499] outline-none">
            {["全部", "<50", "50-80", "80以上", "未知"].map((item) => <option key={item} value={item}>{item === "全部" ? "请选择价格带" : item}</option>)}
          </select>
        </label>
        <label className="grid grid-cols-[80px_1fr] items-center gap-2 text-[14px] font-semibold text-[#172033]">
          进口/国产
          <select value={props.originFilter} onChange={(event) => props.onOriginFilterChange(event.target.value)} className="h-9 min-w-0 rounded-lg border border-[#E5E9F5] bg-white px-4 text-[14px] font-normal text-[#7A8499] outline-none">
            {["全部", "进口/国际品牌", "国产品牌"].map((item) => <option key={item} value={item}>{item === "全部" ? "全部" : item.replace("/国际品牌", "")}</option>)}
          </select>
        </label>
        <button type="button" className="h-9 rounded-[10px] bg-[#3F35FF] px-9 text-[14px] font-semibold text-white shadow-[0_8px_24px_rgba(63,53,255,0.22)] max-[1399px]:col-span-2">搜索</button>
      </div>
    </section>
  );
}

function ProductInfoRows({ product, className = "" }: { product: Product; className?: string }) {
  return (
    <div className={`grid grid-rows-[20px_40px_20px_20px] gap-2.5 leading-5 ${className}`}>
      {[
        ["品牌名", product.brand],
        ["产品名", product.name],
        ["价格带", product.price],
        ["进口/国产", product.origin],
      ].map(([label, value]) => (
        <div key={label} className="grid grid-cols-[66px_minmax(0,1fr)] items-start gap-3">
          <span className="text-[12px] font-normal text-[#7A8499]">{label}</span>
          <span
            className={
              label === "价格带"
                ? "truncate text-[14px] font-semibold text-[#172033]"
                : label === "产品名"
                  ? "overflow-hidden text-[13px] font-medium text-[#172033] [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2]"
                  : "truncate text-[13px] font-medium text-[#172033]"
            }
            title={value}
          >
            {value}
          </span>
        </div>
      ))}
    </div>
  );
}

function ProductCatalogCard({ product, onSelect }: { product: Product; onSelect?: (product: Product) => void }) {
  const className = `relative min-w-[230px] max-w-[250px] flex-1 rounded-2xl border bg-white p-3 text-left shadow-[0_6px_18px_rgba(30,41,59,0.05)] transition ${onSelect ? "hover:-translate-y-0.5 hover:shadow-[0_10px_24px_rgba(30,41,59,0.09)]" : ""} ${product.selected ? "border-[#4B4BFF] ring-2 ring-[#4B4BFF]/10" : "border-[#E5E9F5]"}`;
  const content = (
    <>
      <span className={`absolute right-4 top-3 z-10 flex h-9 w-9 items-center justify-center rounded-full border bg-white/90 shadow-sm ${product.selected ? "border-[#FFD7DA] text-[#FF4D4F]" : "border-[#E5E9F5] text-[#655BFF]"}`}>
        <FavoriteIcon selected={product.selected} />
      </span>
      <div className="h-[150px] overflow-hidden rounded-xl border border-[#EDF0F7] bg-gradient-to-br from-[#F7F9FF] via-white to-[#EEF3FF]">
        <PackageImage product={product} />
      </div>
      <ProductInfoRows product={product} className="mt-4 px-1 pb-1" />
    </>
  );

  if (onSelect) {
    return (
      <button type="button" onClick={() => onSelect(product)} className={className}>
        {content}
      </button>
    );
  }
  return <div className={className}>{content}</div>;
}

function ProductCarousel(props: {
  products: Product[];
  onSelect: (product: Product) => void;
  loading: boolean;
  error: string;
  canPrev: boolean;
  canNext: boolean;
  page: number;
  totalPages: number;
  totalCount: number;
  onPrev: () => void;
  onNext: () => void;
}) {
  const showPager = !props.loading && !props.error && props.totalCount > PRODUCT_CAROUSEL_PAGE_SIZE;
  return (
    <section className="relative rounded-2xl border border-[#E5E9F5] bg-white px-10 py-4 shadow-[0_8px_24px_rgba(30,41,59,0.04)]">
      <button
        type="button"
        onClick={props.onPrev}
        disabled={!props.canPrev}
        aria-label="上一页产品"
        title="上一页产品"
        className="absolute left-4 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-[#E5E9F5] bg-white text-[#4B4BFF] shadow-sm transition hover:border-[#B9B4FF] hover:bg-[#F7F6FF] disabled:cursor-not-allowed disabled:text-[#B4BCD0] disabled:hover:border-[#E5E9F5] disabled:hover:bg-white"
      >
        ‹
      </button>
      <div className="scrollbar-none flex gap-5 overflow-x-auto px-6">
        {props.loading && <div className="py-20 text-[14px] font-normal text-[#7A8499]">正在加载产品库...</div>}
        {props.error && <div className="py-20 text-[14px] font-medium text-[#FF4D4F]">{props.error}</div>}
        {!props.loading && !props.error && props.products.map((product) => (
          <ProductCatalogCard key={product.brand + product.name} product={product} onSelect={props.onSelect} />
        ))}
      </div>
      {showPager && (
        <div className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-white/90 px-3 py-1 text-[11px] font-semibold text-[#7A8499] shadow-sm">
          {props.page + 1} / {props.totalPages}
        </div>
      )}
      <button
        type="button"
        onClick={props.onNext}
        disabled={!props.canNext}
        aria-label="下一页产品"
        title="下一页产品"
        className="absolute right-4 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-[#E5E9F5] bg-white text-[#4B4BFF] shadow-sm transition hover:border-[#B9B4FF] hover:bg-[#F7F6FF] disabled:cursor-not-allowed disabled:text-[#B4BCD0] disabled:hover:border-[#E5E9F5] disabled:hover:bg-white"
      >
        ›
      </button>
    </section>
  );
}

function SelectedCompareBar(props: {
  selectedCount: number;
  selectedProducts: ProductOption[];
  loading: boolean;
  summaryLoading: boolean;
  canCompare: boolean;
  hasResult: boolean;
  onAddProduct: () => void;
  onRemoveProduct: (value: string) => void;
  onCompare: () => void;
  onSummary: () => void;
}) {
  return (
    <section className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] items-start gap-4 max-[1399px]:grid-cols-[minmax(0,1fr)_auto]">
      <div className="flex min-h-[48px] flex-wrap items-center justify-between gap-3 rounded-xl border border-[#E5E9F5] bg-white px-5 py-2 shadow-[0_8px_24px_rgba(30,41,59,0.04)]">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3 text-[#172033]">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[#F3F2FF] text-[#3F35FF]">
            <FavoriteIcon className="h-4 w-4" />
          </span>
          <span className="text-[16px] font-semibold">已选内容（{props.selectedCount}）</span>
          {props.selectedProducts.length ? (
            <div className="flex min-w-0 flex-1 flex-wrap gap-2">
              {props.selectedProducts.map((product) => {
                const value = productOptionValue(product);
                const name = [product.brand, product.product_name].filter(Boolean).join(" ") || product.label;
                return (
                  <button
                    key={value}
                    type="button"
                    onClick={() => props.onRemoveProduct(value)}
                    title={`移除 ${name}`}
                    className="inline-flex max-w-[340px] items-center gap-2 rounded-lg border border-[#DDDFFF] bg-[#F7F6FF] px-3 py-1 text-[12px] font-medium text-[#4034E8] hover:border-[#B9B4FF] hover:bg-[#F0EEFF]"
                  >
                    <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">{name}</span>
                    <span className="text-[14px] leading-none text-[#7A70FF]">×</span>
                  </button>
                );
              })}
            </div>
          ) : (
            <span className="text-[14px] font-normal text-[#8A94A8]">还未选择任何产品，点击商品卡片加入对比</span>
          )}
          <span className="text-[14px] font-normal text-[#8A94A8]">
            {props.loading ? "正在生成对比" : props.selectedCount >= 2 ? "已准备好对比" : props.selectedCount === 1 ? "请选择 2 个产品" : ""}
          </span>
        </div>
        <span className="text-[#4B4BFF]"><ChevronDownIcon /></span>
      </div>
      <button type="button" onClick={props.onAddProduct} className="h-10 rounded-[10px] border border-[#4B4BFF] px-8 text-[14px] font-semibold text-[#3F35FF]">＋ 没找到？添加产品</button>
      {props.hasResult ? (
        <>
          <button type="button" onClick={props.onSummary} disabled={props.summaryLoading} className="h-10 rounded-[10px] border border-[#4B4BFF] px-8 text-[14px] font-semibold text-[#3F35FF] disabled:cursor-not-allowed disabled:opacity-50">▣ {props.summaryLoading ? "生成中" : "换粮总结"}</button>
          <button type="button" className="h-10 rounded-[10px] bg-[#3F35FF] px-8 text-[14px] font-semibold text-white shadow-[0_8px_24px_rgba(63,53,255,0.22)]">⌄ 收起对比 ⌃</button>
        </>
      ) : (
        <button
          type="button"
          onClick={props.onCompare}
          disabled={!props.canCompare || props.loading}
          className="h-10 rounded-[10px] bg-[#3F35FF] px-10 text-[14px] font-semibold text-white shadow-[0_8px_24px_rgba(63,53,255,0.22)] disabled:cursor-not-allowed disabled:bg-[#E8EBF2] disabled:text-[#B4BCD0] disabled:shadow-none"
        >
          {props.loading ? "对比中" : "对比一下"}
        </button>
      )}
    </section>
  );
}

type AddProductFormState = {
  brandName: string;
  productName: string;
  images: File[];
};

function AddProductModal(props: {
  open: boolean;
  onClose: () => void;
}) {
  const [form, setForm] = useState<AddProductFormState>({ brandName: "", productName: "", images: [] });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  useEffect(() => {
    if (!props.open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) props.onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [props.open, props.onClose, submitting]);

  function resetAndClose() {
    if (submitting) return;
    setForm({ brandName: "", productName: "", images: [] });
    setError("");
    setSuccess("");
    props.onClose();
  }

  async function waitForImageParse(taskId: string) {
    for (let index = 0; index < 20; index += 1) {
      const response = await fetch(`/api/cat-food/tasks/${taskId}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "产品解析状态加载失败");
      if (data.task?.status === "failed") throw new Error(data.task.error_message || "产品资料解析失败");
      if (data.task?.status === "success") return data.task;
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    return null;
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const brandName = form.brandName.trim();
    const productName = form.productName.trim();
    if (!brandName) {
      setError("请填写品牌名。");
      return;
    }
    if (!productName) {
      setError("请填写产品名。");
      return;
    }
    if (!form.images.length) {
      setError("请上传包含配料表和营养成分保证值的产品图片。");
      return;
    }
    if (form.images.some((image) => !image.type.startsWith("image/"))) {
      setError("仅支持 JPG、PNG、WEBP 等图片格式。");
      return;
    }
    if (form.images.some((image) => image.size > 10 * 1024 * 1024)) {
      setError("每张图片大小不能超过 10MB。");
      return;
    }

    setSubmitting(true);
    setError("");
    setSuccess("");
    try {
      const taskResponse = await fetch("/api/cat-food/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: "image_parse",
          current_food: `${brandName} ${productName}`,
          notes: "用户从猫粮对比页提交的新产品",
        }),
      });
      const taskData = await taskResponse.json();
      if (!taskResponse.ok) throw new Error(taskData.error || "产品任务创建失败");
      const taskId = taskData.task?.id;
      if (!taskId) throw new Error("产品任务创建失败：缺少任务编号");

      const uploadBody = new FormData();
      uploadBody.append("brand_name", brandName);
      uploadBody.append("product_name", productName);
      form.images.forEach((image) => uploadBody.append("images", image));
      const uploadResponse = await fetch(`/api/cat-food/tasks/${taskId}/image-batch`, {
        method: "POST",
        body: uploadBody,
      });
      const uploadData = await uploadResponse.json();
      if (!uploadResponse.ok) throw new Error(uploadData.error || "产品图片上传失败");

      const parsedTask = await waitForImageParse(taskId);
      setSuccess(
        parsedTask
          ? "提交成功。产品资料已完成初步入库，待营养指标审核完成后会进入可对比产品库。"
          : "提交成功。产品资料正在后台解析，可稍后在产品库中搜索。",
      );
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "产品提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  if (!props.open) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-[#172033]/45 px-4 py-8 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-labelledby="add-product-title">
      <div className="max-h-full w-full max-w-[620px] overflow-y-auto rounded-2xl border border-[#E1E6F0] bg-white shadow-[0_24px_80px_rgba(23,32,51,0.24)]">
        <div className="flex items-start justify-between gap-4 border-b border-[#EDF0F7] px-6 py-5">
          <div>
            <h2 id="add-product-title" className="text-[20px] font-bold text-[#172033]">添加产品</h2>
            <p className="mt-1 text-[12px] leading-5 text-[#7A8499]">提交品牌、产品名称，以及包含配料表和营养成分保证值的图片；内容分布在不同包装面时可上传多张。</p>
          </div>
          <button type="button" onClick={resetAndClose} disabled={submitting} aria-label="关闭" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#F3F5FA] text-[20px] text-[#657087] hover:bg-[#E9EDF5] disabled:cursor-not-allowed">×</button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-5 px-6 py-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="space-y-2">
              <span className="text-[13px] font-semibold text-[#253047]">品牌名 <i className="not-italic text-[#FF4D4F]">*</i></span>
              <input
                value={form.brandName}
                onChange={(event) => setForm((value) => ({ ...value, brandName: event.target.value }))}
                disabled={submitting || Boolean(success)}
                maxLength={100}
                placeholder="例如：皇家"
                className="h-11 w-full rounded-xl border border-[#DDE3EE] px-4 text-[14px] text-[#172033] outline-none transition focus:border-[#5145FF] focus:ring-4 focus:ring-[#5145FF]/10 disabled:bg-[#F6F7FA]"
              />
            </label>
            <label className="space-y-2">
              <span className="text-[13px] font-semibold text-[#253047]">产品名 <i className="not-italic text-[#FF4D4F]">*</i></span>
              <input
                value={form.productName}
                onChange={(event) => setForm((value) => ({ ...value, productName: event.target.value }))}
                disabled={submitting || Boolean(success)}
                maxLength={200}
                placeholder="例如：K36 室内成猫粮"
                className="h-11 w-full rounded-xl border border-[#DDE3EE] px-4 text-[14px] text-[#172033] outline-none transition focus:border-[#5145FF] focus:ring-4 focus:ring-[#5145FF]/10 disabled:bg-[#F6F7FA]"
              />
            </label>
          </div>

          <label className="block space-y-2">
            <span className="text-[13px] font-semibold text-[#253047]">产品图片 <i className="not-italic text-[#FF4D4F]">*</i></span>
            <span className={`flex min-h-[132px] cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed px-5 py-5 text-center transition ${form.images.length ? "border-[#867EFF] bg-[#F7F6FF]" : "border-[#C9D1DF] bg-[#FAFBFD] hover:border-[#867EFF] hover:bg-[#F8F7FF]"} ${submitting || success ? "pointer-events-none opacity-70" : ""}`}>
              <input
                type="file"
                multiple
                accept="image/jpeg,image/png,image/webp"
                className="sr-only"
                disabled={submitting || Boolean(success)}
                onChange={(event) => {
                  const selected = Array.from(event.target.files || []);
                  setForm((value) => {
                    const merged = [...value.images];
                    selected.forEach((image) => {
                      const key = `${image.name}:${image.size}:${image.lastModified}`;
                      const exists = merged.some((item) => `${item.name}:${item.size}:${item.lastModified}` === key);
                      if (!exists && merged.length < 3) merged.push(image);
                    });
                    return { ...value, images: merged };
                  });
                  if (form.images.length + selected.length > 3) setError("一次最多上传 3 张图片。");
                  else setError("");
                  event.target.value = "";
                }}
              />
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#EEEAFE] text-[22px] text-[#5145FF]">＋</span>
              <span className="mt-3 text-[13px] font-semibold text-[#35415A]">{form.images.length ? `已选择 ${form.images.length} 张：${form.images.map((image) => image.name).join("、")}` : "点击上传配料表和营养成分保证值图片"}</span>
              <span className="mt-1 text-[11px] text-[#8A94A8]">支持 1～3 张 JPG、PNG、WEBP，单张不超过 10MB</span>
            </span>
          </label>

          {form.images.length > 0 && (
            <div className="grid gap-2 sm:grid-cols-3">
              {form.images.map((image, index) => (
                <div key={`${image.name}:${image.size}:${image.lastModified}`} className="flex min-w-0 items-center gap-2 rounded-lg border border-[#E1E6F0] bg-white px-3 py-2">
                  <span className="min-w-0 flex-1 truncate text-[11px] text-[#59657A]" title={image.name}>{index + 1}. {image.name}</span>
                  <button
                    type="button"
                    disabled={submitting || Boolean(success)}
                    onClick={() => setForm((value) => ({ ...value, images: value.images.filter((_, itemIndex) => itemIndex !== index) }))}
                    className="shrink-0 text-[16px] font-bold text-[#A0A8BA] hover:text-[#FF4D4F] disabled:cursor-not-allowed"
                    aria-label={`删除 ${image.name}`}
                  >×</button>
                </div>
              ))}
            </div>
          )}

          <div className="rounded-xl bg-[#F4F7FC] px-4 py-3 text-[12px] leading-5 text-[#657087]">
            必须同时包含完整配料表和营养成分保证值；若两者在不同包装面，请分别拍摄并一起上传。新产品完成审核后才能参与对比。
          </div>

          <a
            href="/products/cat-food-upload-example.png"
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-4 rounded-xl border border-[#E1E6F0] bg-white p-3 transition hover:border-[#867EFF] hover:bg-[#FAF9FF]"
          >
            <img
              src="/products/cat-food-upload-example.png"
              alt="配料表和营养成分保证值合格图片示例"
              className="h-24 w-20 shrink-0 rounded-lg border border-[#E1E6F0] object-cover object-top"
            />
            <span className="min-w-0">
              <strong className="block text-[13px] text-[#253047]">合格图片示例</strong>
              <span className="mt-1 block text-[12px] leading-5 text-[#7A8499]">红框中的“原料组成”和“产品分析保证值”需清晰可读；不在同一张图时请分别上传。点击查看大图。</span>
            </span>
          </a>

          {error && <div className="rounded-xl border border-[#FFD4D6] bg-[#FFF1F2] px-4 py-3 text-[12px] font-medium text-[#D93B3E]">{error}</div>}
          {success && <div className="rounded-xl border border-[#CDEAC8] bg-[#F1FAEF] px-4 py-3 text-[12px] font-medium leading-5 text-[#34822C]">{success}</div>}

          <div className="flex justify-end gap-3 border-t border-[#EDF0F7] pt-5">
            <button type="button" onClick={resetAndClose} disabled={submitting} className="h-10 rounded-xl border border-[#DDE3EE] px-6 text-[13px] font-semibold text-[#59657A] disabled:cursor-not-allowed">
              {success ? "完成" : "取消"}
            </button>
            {!success && (
              <button type="submit" disabled={submitting} className="h-10 min-w-[132px] rounded-xl bg-[#4034E8] px-6 text-[13px] font-semibold text-white shadow-[0_8px_20px_rgba(64,52,232,0.22)] disabled:cursor-not-allowed disabled:bg-[#AAA5E8]">
                {submitting ? "正在提交..." : "提交产品"}
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

function EmptyComparePanel({ selectedCount }: { selectedCount: number }) {
  return (
    <section className="flex min-h-[300px] flex-col items-center justify-center rounded-2xl border border-dashed border-[#DDE4F2] bg-white text-center">
      <div className="mb-5 flex h-20 w-20 items-center justify-center rounded-2xl bg-[#F3F2FF] text-[#6F63FF]">
        <EmptyCompareIcon />
      </div>
      <div className="text-[16px] font-semibold text-[#172033]">{selectedCount ? `已选择 ${selectedCount} 个产品` : "暂未选择产品"}</div>
      <div className="mt-3 text-[14px] font-normal text-[#7A8499]">点击商品卡片右上角收藏，将产品加入对比</div>
    </section>
  );
}

type IngredientRole = {
  name: string;
  tags: string[];
  tone: "blue" | "violet" | "orange" | "green" | "cyan";
};

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text || !["[", "{"].includes(text[0])) return value;
  try {
    return JSON.parse(text);
  } catch {
    return value;
  }
}

function collectIngredientNames(value: unknown): string[] {
  const parsed = parseMaybeJson(value);
  if (parsed === null || parsed === undefined || parsed === "") return [];
  if (Array.isArray(parsed)) return parsed.flatMap(collectIngredientNames);
  if (typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    for (const key of ["ingredient_name", "name", "ingredient", "standard_name"]) {
      if (record[key]) return collectIngredientNames(record[key]);
    }
    return Object.values(record).flatMap(collectIngredientNames);
  }
  return String(parsed)
    .split(/[、,，;；|/\n]+/)
    .map((item) => item.trim())
    .filter((item) => item && !["暂无", "无", "none", "null"].includes(item.toLowerCase()));
}

function uniqueIngredients(values: string[], limit = 8): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const cleaned = value.replace(/^[\[\]{}"'：:\s]+|[\[\]{}"'：:\s]+$/g, "").trim();
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    result.push(cleaned);
    if (result.length >= limit) break;
  }
  return result;
}

function fiberIngredients(evidence?: MaterialRoleEvidence): string[] {
  const fiber = evidence?.fiber_carb_roles || {};
  const feature = parseMaybeJson(fiber.ingredient_feature_json);
  if (!feature || typeof feature !== "object" || Array.isArray(feature)) return [];
  const record = feature as Record<string, unknown>;
  const subtypeTags = parseMaybeJson(record.ingredient_subtype_tags);
  if (subtypeTags && typeof subtypeTags === "object" && !Array.isArray(subtypeTags)) {
    return uniqueIngredients(Object.values(subtypeTags as Record<string, unknown>).flatMap(collectIngredientNames));
  }
  const detail = parseMaybeJson(record.ingredient_tag_detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return uniqueIngredients(Object.keys(detail as Record<string, unknown>));
  }
  return [];
}

function originalIngredientSegments(evidence?: MaterialRoleEvidence): string[] {
  const raw = evidence?.raw_ingredient_text;
  if (typeof raw !== "string") return [];
  return raw.split(/[、,，;；\n]+/).map((item) => item.trim()).filter(Boolean);
}

function originalRoleIngredients(evidence?: MaterialRoleEvidence): Record<string, string[]> | null {
  const items = evidence?.ingredient_items;
  if (!Array.isArray(items) || !items.length) return null;
  const segments = originalIngredientSegments(evidence);
  const roles: Record<string, string[]> = { protein: [], starch: [], fat: [], fiber: [], protection: [] };
  const add = (key: string, value: string) => {
    if (value && !roles[key].includes(value)) roles[key].push(value);
  };
  for (const item of items) {
    const position = Number(item.position || 0);
    const original = segments[position - 1] || item.raw_name || item.standard_name || "";
    if (!original) continue;
    const role = String(item.primary_nutrition_role || "");
    const family = String(item.ingredient_family || "");
    const features = parseMaybeJson(item.features_json);
    const featureKeys = features && typeof features === "object" && !Array.isArray(features)
      ? Object.keys(features as Record<string, unknown>)
      : [];
    const hasFeature = (prefix: string) => featureKeys.some((key) => key.startsWith(prefix));
    const hasExplicitFatSource = featureKeys.includes("fat.fat_sources");
    if (Boolean(item.is_protein) || role.includes("蛋白")) add("protein", original);
    if (role.includes("碳水") || family.includes("淀粉") || hasFeature("starch.")) add("starch", original);
    if (role.includes("脂肪") || family.includes("油脂") || hasExplicitFatSource) add("fat", original);
    if (role.includes("纤维") || role.includes("草本功能") || family.includes("纤维") || hasFeature("fiber.")) add("fiber", original);
    if (role.includes("抗氧化") || role.includes("微量") || role.includes("皮肤") || hasFeature("antioxidant.")) add("protection", original);
  }
  return roles;
}

function buildIngredientRoles(evidence?: MaterialRoleEvidence): IngredientRole[] {
  const originalRoles = originalRoleIngredients(evidence);
  const protein = evidence?.protein_roles || {};
  const proteinScoreRules = evidence?.protein_score_rules || {};
  const fat = evidence?.fat_roles || {};
  const fiber = evidence?.fiber_carb_roles || {};
  const standardizedAnimalSources = uniqueIngredients([
    ...collectIngredientNames(protein.animal_source_level2_sources),
    ...collectIngredientNames(proteinScoreRules.animal_source_level2_sources),
  ]);
  const fallbackAnimalSources = uniqueIngredients([
    ...collectIngredientNames(protein.animal_sources),
    ...collectIngredientNames(proteinScoreRules.animal_sources),
    ...collectIngredientNames(protein.primary_meat_source_species),
    ...collectIngredientNames(protein.secondary_meat_source_species),
  ]);
  const plantProteinSources = uniqueIngredients([
    ...collectIngredientNames(protein.plant_protein_labels),
    ...collectIngredientNames(proteinScoreRules.plant_protein_labels),
  ]);
  const proteinTags = uniqueIngredients([
    ...(standardizedAnimalSources.length ? standardizedAnimalSources : fallbackAnimalSources),
    ...plantProteinSources,
  ]);
  const starchTags = uniqueIngredients(collectIngredientNames(fiber.starch_ingredients_json));
  const fatTags = uniqueIngredients(collectIngredientNames(fat.fat_sources));
  const fiberTags = fiberIngredients(evidence);
  const protectionTags = uniqueIngredients([
    ...collectIngredientNames(fat.antioxidant_sources),
    ...collectIngredientNames(fat.micronutrient_sources),
    ...collectIngredientNames(fat.omega3_sources),
  ]);
  return [
    { name: "蛋白来源", tags: originalRoles?.protein.length ? uniqueIngredients(originalRoles.protein, 20) : proteinTags, tone: "blue" },
    { name: "碳水和淀粉来源", tags: originalRoles?.starch.length ? uniqueIngredients(originalRoles.starch, 20) : starchTags, tone: "violet" },
    { name: "脂肪来源", tags: originalRoles?.fat.length ? uniqueIngredients(originalRoles.fat, 20) : fatTags, tone: "orange" },
    { name: "肠道支持和纤维缓冲", tags: originalRoles?.fiber.length ? uniqueIngredients(originalRoles.fiber, 20) : fiberTags, tone: "green" },
    { name: "抗氧化与皮肤屏障支持", tags: originalRoles?.protection.length ? uniqueIngredients(originalRoles.protection, 20) : protectionTags, tone: "cyan" },
  ];
}

const nutritionFallbacks: Record<string, number> = {
  蛋白质量: 84,
  纤维缓冲: 14.24,
  菌群支持: 0.32,
  皮肤保护: 17.9,
  蛋白压力: 21,
  碳水负担: 75.6,
  脂肪负担: 50.8,
};

function SectionTitle({ icon, children, info = false }: { icon: string; children: React.ReactNode; info?: boolean }) {
  return (
    <h3 className="mb-2 flex items-center gap-2 text-[16px] font-bold text-[#172033]">
      <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#EEF6E9] text-[16px] text-[#4B962F]">{icon}</span>
      <span>{children}</span>
      {info && <span className="flex h-5 w-5 items-center justify-center rounded-full border border-[#AAB5CA] text-[11px] font-semibold text-[#8A94A8]">i</span>}
    </h3>
  );
}

function ProductInfoPanel({ product }: { product: Product }) {
  const rows = [
    ["品牌名", product.brand],
    ["产品名", product.name],
    ["价格带", product.price],
    ["进口/国产", product.origin],
  ];
  return (
    <section className="h-[360px] rounded-2xl border border-[#E5E9F5] bg-white p-3 shadow-[0_8px_22px_rgba(30,41,59,0.05)]">
      <div className="relative h-[150px] overflow-hidden rounded-xl bg-[#F5F7FF]">
        <PackageImage product={product} />
        <span className="absolute right-3 top-3 flex h-10 w-10 items-center justify-center rounded-full border border-[#FFD7DA] bg-white/95 text-[#FF4D4F] shadow-sm">
          <FavoriteIcon selected className="h-5 w-5" />
        </span>
      </div>
      <div className="mt-3">
        {rows.map(([label, value], index) => (
          <div key={label} className={`grid min-h-[45px] grid-cols-[72px_minmax(0,1fr)] items-center gap-3 px-1 ${index < rows.length - 1 ? "border-b border-[#EDF0F5]" : ""}`}>
            <span className="text-[12px] font-normal text-[#7A8499]">{label}</span>
            <span className={`${label === "价格带" ? "text-[14px] font-semibold" : "text-[13px] font-medium"} overflow-hidden text-ellipsis text-[#172033] [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2]`} title={value}>
              {value}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function IngredientPreviewPanel({ product }: { product: Product }) {
  const evidenceText = product.materialRoleEvidence?.raw_ingredient_text;
  const ingredients = product.ingredients && product.ingredients !== "暂无原料信息"
    ? product.ingredients
    : typeof evidenceText === "string" && evidenceText.trim()
      ? evidenceText
      : "暂无原料信息";
  return (
    <section className="rounded-2xl border border-[#E8EDF6] bg-white px-4 py-3 shadow-[0_6px_18px_rgba(30,41,59,0.04)]">
      <SectionTitle icon="🌿">原料预览</SectionTitle>
      <div className="rounded-xl border border-[#DCE9D2] bg-gradient-to-r from-[#F8FAF3] to-[#F3F7ED] px-4 py-2 text-[12px] font-normal leading-5 text-[#374151]">
        {ingredients}
      </div>
    </section>
  );
}

function IngredientRolePanel({ product }: { product: Product }) {
  const tones = {
    blue: ["bg-[#EAF3FF] text-[#176DFF]", "border-[#DCEAFF] bg-[#F6F9FF]"],
    violet: ["bg-[#F1ECFF] text-[#7447FF]", "border-[#E8E0FF] bg-[#FAF8FF]"],
    orange: ["bg-[#FFF0E5] text-[#F97316]", "border-[#FFE2CC] bg-[#FFF9F4]"],
    green: ["bg-[#EDF7E8] text-[#409624]", "border-[#DCEED3] bg-[#F8FCF5]"],
    cyan: ["bg-[#E8F7F7] text-[#168A91]", "border-[#D7EEEE] bg-[#F6FBFB]"],
  } as const;
  const roles = buildIngredientRoles(product.materialRoleEvidence);
  return (
    <section className="rounded-2xl border border-[#E8EDF6] bg-white px-4 py-3 shadow-[0_6px_18px_rgba(30,41,59,0.04)]">
      <SectionTitle icon="✣">原材料营养角色分类</SectionTitle>
      <div className="grid grid-cols-2 gap-2">
        {roles.map((role, index) => {
          const [badgeClass, rowClass] = tones[role.tone];
          return (
            <div key={role.name} className={`rounded-xl border px-3 py-2 ${index === roles.length - 1 ? "col-span-2" : ""} ${rowClass}`}>
              <div className="mb-1 flex items-center gap-2">
                <span className={`flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold ${badgeClass}`}>{String(index + 1).padStart(2, "0")}</span>
                <span className="text-[12px] font-semibold text-[#35415A]">{role.name}</span>
              </div>
              <div className="flex flex-wrap gap-1 pl-8">
                {role.tags.length
                  ? role.tags.map((tag) => <span key={tag} className="rounded-md border border-current/10 bg-white/70 px-2 py-0.5 text-[10px] font-medium leading-3 text-[#59657A]">{tag}</span>)
                  : <span className="text-[10px] font-normal text-[#9AA3B4]">暂无识别结果</span>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function NutritionMetricCard({ label, value, kind }: { label: string; value: number; kind: "positive" | "negative" }) {
  const positive = kind === "positive";
  return (
    <div className={`flex min-h-[50px] items-center justify-between gap-2 rounded-lg border px-2 py-1.5 ${positive ? "border-[#D7EBD0] bg-white" : "border-[#F4DDC8] bg-white"}`}>
      <div className="flex items-center gap-2">
        <span className={`flex h-6 w-6 items-center justify-center rounded-md text-[13px] ${positive ? "bg-[#EEF8EA] text-[#409624]" : "bg-[#FFF1E6] text-[#F97316]"}`}>
          {positive ? "＋" : "−"}
        </span>
        <span className="text-[11px] font-medium text-[#4B5563]">{label}</span>
      </div>
      <div className={`text-[16px] font-semibold leading-none ${positive ? "text-[#245E35]" : "text-[#9A4A16]"}`}>{value}</div>
    </div>
  );
}

function NutritionScorePanel({ product }: { product: Product }) {
  const scoreValue = (label: string) => product.scores.find((score) => score.label === label)?.value ?? nutritionFallbacks[label];
  const positiveLabels = ["蛋白质量", "纤维缓冲", "菌群支持", "皮肤保护"];
  const negativeLabels = ["蛋白压力", "碳水负担", "脂肪负担"];
  return (
    <section className="h-full overflow-hidden rounded-2xl border border-[#E8EDF6] bg-white p-2 shadow-[0_6px_18px_rgba(30,41,59,0.04)]">
      <SectionTitle icon="✣" info>营养得分</SectionTitle>
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded-lg border border-[#D8EACA] bg-[#F7FBF3] p-2">
          <div className="mb-1.5 inline-flex items-center gap-1 rounded-full bg-[#EAF6E4] px-2 py-0.5 text-[10px] font-semibold text-[#3E8B2B]">
            <span>＋</span> 加分项
          </div>
          <div className="grid grid-cols-4 gap-1">
            {positiveLabels.map((label) => <NutritionMetricCard key={label} label={label} value={scoreValue(label)} kind="positive" />)}
          </div>
        </div>
        <div className="rounded-lg border border-[#F3DCC4] bg-[#FFF8F1] p-2">
          <div className="mb-1.5 inline-flex items-center gap-1 rounded-full bg-[#FFF0E1] px-2 py-0.5 text-[10px] font-semibold text-[#E66A15]">
            <span>−</span> 减分项
          </div>
          <div className="grid grid-cols-3 gap-1">
            {negativeLabels.map((label) => <NutritionMetricCard key={label} label={label} value={scoreValue(label)} kind="negative" />)}
          </div>
        </div>
      </div>
    </section>
  );
}

function ProductAnalysisCard({ product }: { product: Product }) {
  return (
    <article className="min-w-[1120px] rounded-2xl border border-[#DDE5F3] bg-white p-4 shadow-[0_10px_28px_rgba(30,41,59,0.06)]">
      <div className="grid grid-cols-[260px_minmax(0,1fr)] items-start gap-4 max-[1439px]:grid-cols-[240px_minmax(0,1fr)]">
        <ProductInfoPanel product={product} />
        <div className="space-y-2">
          <IngredientPreviewPanel product={product} />
          <IngredientRolePanel product={product} />
        </div>
      </div>
    </article>
  );
}

function CompareResultSection({ products }: { products: Product[] }) {
  return (
    <section className="scrollbar-none space-y-4 overflow-x-auto">
      {products.map((product) => <ProductAnalysisCard key={product.brand + product.name} product={product} />)}
    </section>
  );
}

const comparisonChartDimensions = [
  {
    label: "蛋白质量",
    type: "positive",
    explanation: "衡量蛋白来源质量、动物蛋白优势和蛋白正向支持，不等同于蛋白压力低。",
    factors: "动物蛋白占优程度、肉源或动物来源清晰度、主要蛋白形态（鲜肉、冻肉、肉粉、水解蛋白等）、植物蛋白干扰程度、粗蛋白含量适配度。",
  },
  {
    label: "纤维缓冲",
    type: "positive",
    explanation: "衡量纤维结构是否有助于便便成形并提供肠道缓冲，不只看粗纤维总量。",
    factors: "纤维来源多样性、可溶与不可溶纤维搭配、粪便骨架支持、肠道刺激缓冲能力、粗纤维含量适配度。",
  },
  {
    label: "菌群支持",
    type: "positive",
    explanation: "衡量供菌底物和菌群代谢支持是否平衡；供菌原料多不代表一定更稳定。",
    factors: "益生元及供菌底物、短链脂肪酸代谢支持、发酵稳定性、底物数量与结构、过量发酵压力。",
  },
  {
    label: "皮肤保护",
    type: "positive",
    explanation: "衡量脂肪调节、皮肤屏障稳定和氧化压力缓冲方面的配方支持。",
    factors: "Omega 脂肪酸来源、抗氧化支持、微量营养素、脂肪调节原料、皮肤屏障相关营养支持。",
  },
  {
    label: "蛋白压力",
    type: "pressure",
    explanation: "衡量蛋白来源和蛋白结构的复杂程度，以及可能带来的消化和换粮适应压力。",
    factors: "肉源数量、蛋白来源复杂度、多肉源叠加、植物蛋白或豆类参与、主要蛋白形态、蛋白结构负载。",
  },
  {
    label: "碳水负担",
    type: "pressure",
    explanation: "衡量淀粉、豆类和薯类等碳水结构的配方压力，不代表保证值中的直接碳水含量。",
    factors: "淀粉原料占比与排序、豆类和薯类参与、碳水来源复杂度、发酵压力、软便相关结构风险。",
  },
  {
    label: "脂肪负担",
    type: "pressure",
    explanation: "衡量油脂结构和脂肪消化压力，分数越高越需要关注皮脂、黑下巴和脂肪适应情况。",
    factors: "油脂来源与数量、动物脂肪参与、粗脂肪含量适配度、脂肪来源复杂度、脂肪消化和皮脂压力。",
  },
] as const;

function chartProductName(info: ProductInfo) {
  return [info.brand_name, info.name].filter(Boolean).join(" ") || info.query;
}

function NutritionBurdenComparisonChart({ result }: { result: CompareResult }) {
  const [selectedMetric, setSelectedMetric] = useState<(typeof comparisonChartDimensions)[number]["label"]>("蛋白质量");
  const width = 1160;
  const height = 430;
  const margin = { top: 58, right: 28, bottom: 72, left: 62 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const zeroY = margin.top + plotHeight / 2;
  const yForValue = (value: number) => zeroY - (value / 100) * (plotHeight / 2);
  const groupWidth = plotWidth / comparisonChartDimensions.length;
  const barWidth = Math.min(30, groupWidth * 0.23);
  const currentName = chartProductName(result.current_food);
  const targetName = chartProductName(result.target_food);
  const ticks = [100, 75, 50, 25, 0, -25, -50, -75, -100];
  const selectedMetricInfo = comparisonChartDimensions.find((item) => item.label === selectedMetric) || comparisonChartDimensions[0];

  const series = comparisonChartDimensions.map((dimension) => {
    const currentRaw = profileScore(result.current_food.profile, dimension.label) ?? 0;
    const targetRaw = profileScore(result.target_food.profile, dimension.label) ?? 0;
    const direction = dimension.type === "pressure" ? -1 : 1;
    return {
      ...dimension,
      current: Math.max(-100, Math.min(100, currentRaw * direction)),
      target: Math.max(-100, Math.min(100, targetRaw * direction)),
    };
  });

  return (
    <section className="overflow-hidden rounded-2xl border border-[#E1E6F0] bg-white shadow-[0_8px_24px_rgba(30,41,59,0.05)]">
      <div className="flex flex-wrap items-start justify-between gap-4 px-5 pb-1 pt-5">
        <div>
          <h2 className="flex items-center gap-3 text-[20px] font-bold text-[#172033]">
            <span className="text-[#5145FF]">✦</span>
            营养支持与配方负担对比
            <span className="flex h-5 w-5 items-center justify-center rounded-full border border-[#AAB5CA] text-[11px] font-semibold text-[#8A94A8]">i</span>
          </h2>
          <p className="mt-2 text-[12px] text-[#667189]">上方为营养支持项，越高越好；下方为配方负担项，越接近 0 压力越轻。</p>
        </div>
        <div className="flex flex-wrap gap-6 pt-2 text-[12px] font-medium text-[#4B5563]">
          <span className="flex items-center gap-2"><i className="h-3.5 w-3.5 rounded-[3px] bg-[#1677FF]" />{currentName}</span>
          <span className="flex items-center gap-2"><i className="h-3.5 w-3.5 rounded-[3px] bg-[#FF7900]" />{targetName}</span>
        </div>
      </div>

      <div className="overflow-x-auto px-3">
        <svg viewBox={`0 0 ${width} ${height}`} className="min-w-[900px] w-full" role="img" aria-label={`${currentName}与${targetName}营养支持及配方负担柱状对比图`}>
          {ticks.map((tick) => {
            const y = yForValue(tick);
            return (
              <g key={tick}>
                <line
                  x1={margin.left}
                  x2={width - margin.right}
                  y1={y}
                  y2={y}
                  stroke={tick === 0 ? "#7F899D" : "#DCE2EC"}
                  strokeWidth={tick === 0 ? 1.5 : 1}
                  strokeDasharray={tick === 0 ? undefined : "4 4"}
                />
                <text x={margin.left - 12} y={y + 4} textAnchor="end" fontSize="12" fill="#516078">{tick}</text>
              </g>
            );
          })}
          <text
            x="18"
            y={margin.top + plotHeight / 2}
            transform={`rotate(-90 18 ${margin.top + plotHeight / 2})`}
            textAnchor="middle"
            fontSize="12"
            fontWeight="600"
            fill="#35415A"
          >
            得分（分值）
          </text>

          {series.map((item, index) => {
            const centerX = margin.left + groupWidth * index + groupWidth / 2;
            const bars = [
              { key: "current", value: item.current, x: centerX - barWidth - 3, color: "#1677FF" },
              { key: "target", value: item.target, x: centerX + 3, color: "#FF7900" },
            ];
            return (
              <g key={item.label}>
                {bars.map((bar) => {
                  const valueY = yForValue(bar.value);
                  const y = Math.min(zeroY, valueY);
                  const barHeight = Math.max(1, Math.abs(zeroY - valueY));
                  const labelY = bar.value >= 0 ? y - 8 : y + barHeight + 17;
                  return (
                    <g key={bar.key}>
                      <rect x={bar.x} y={y} width={barWidth} height={barHeight} rx="4" fill={bar.color} />
                      <text x={bar.x + barWidth / 2} y={labelY} textAnchor="middle" fontSize="12" fontWeight="600" fill={bar.color}>
                        {bar.value.toFixed(1)}
                      </text>
                    </g>
                  );
                })}
                <g
                  role="button"
                  tabIndex={0}
                  aria-label={`查看${item.label}解释`}
                  className="cursor-pointer outline-none"
                  onClick={() => setSelectedMetric(item.label)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") setSelectedMetric(item.label);
                  }}
                >
                  <rect
                    x={centerX - groupWidth / 2 + 5}
                    y={height - 57}
                    width={groupWidth - 10}
                    height="32"
                    rx="8"
                    fill={selectedMetric === item.label ? "#F0EEFF" : "transparent"}
                  />
                  <text
                    x={centerX - 5}
                    y={height - 35}
                    textAnchor="middle"
                    fontSize="13"
                    fontWeight="600"
                    fill={selectedMetric === item.label ? "#4034E8" : "#35415A"}
                  >
                    {item.label}
                  </text>
                  <circle cx={centerX + 34} cy={height - 40} r="7" fill="none" stroke={selectedMetric === item.label ? "#5145FF" : "#9AA5B7"} strokeWidth="1" />
                  <text x={centerX + 34} y={height - 37} textAnchor="middle" fontSize="9" fontWeight="700" fill={selectedMetric === item.label ? "#5145FF" : "#7A8499"}>i</text>
                </g>
              </g>
            );
          })}
        </svg>
      </div>

      <div className="mx-5 mb-3 rounded-xl border border-[#DDE3EF] bg-white px-5 py-4 shadow-[0_4px_14px_rgba(30,41,59,0.04)]">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-[16px] font-bold text-[#253047]">{selectedMetricInfo.label}</h3>
          <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${selectedMetricInfo.type === "pressure" ? "bg-[#FFF0E2] text-[#E66A15]" : "bg-[#EAF7E7] text-[#3E942F]"}`}>
            {selectedMetricInfo.type === "pressure" ? "配方负担项 · 越低越好" : "营养支持项 · 越高越好"}
          </span>
        </div>
        <div className="grid gap-2 text-[13px] leading-6 text-[#536078]">
          <p><strong className="font-semibold text-[#35415A]">指标解释：</strong>{selectedMetricInfo.explanation}</p>
          <p><strong className="font-semibold text-[#35415A]">得分因子：</strong>{selectedMetricInfo.factors}</p>
        </div>
      </div>

      <div className="mx-5 mb-4 flex items-start gap-2 rounded-lg border border-[#E2E8F5] bg-[#F3F6FC] px-4 py-2.5 text-[12px] leading-5 text-[#59657A]">
        <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-[#1677FF] text-[10px] font-bold text-[#1677FF]">i</span>
        <span>说明：上方为营养支持项；下方为配方负担项。负担项柱子越短、越接近 0，代表配方压力越轻。</span>
      </div>
    </section>
  );
}

const adviceToneStyles: Record<AdviceTone, {
  dot: string;
  tag: string;
  check: string;
  accent: string;
}> = {
  positive: {
    dot: "bg-[#2E9B2D]",
    tag: "bg-[#EAF7E7] text-[#3E942F]",
    check: "border-[#72C56A] text-[#3E942F]",
    accent: "text-[#2E9B2D]",
  },
  watch: {
    dot: "bg-[#1677FF]",
    tag: "bg-[#EAF3FF] text-[#1677FF]",
    check: "border-[#65A8FF] text-[#1677FF]",
    accent: "text-[#1677FF]",
  },
  caution: {
    dot: "bg-[#FF7900]",
    tag: "bg-[#FFF0E2] text-[#F36D00]",
    check: "border-[#FFAA61] text-[#F36D00]",
    accent: "text-[#F36D00]",
  },
  danger: {
    dot: "bg-[#FF4D4F]",
    tag: "bg-[#FFEDEE] text-[#E83C3F]",
    check: "border-[#FF8B8D] text-[#E83C3F]",
    accent: "text-[#E83C3F]",
  },
};

function AdviceHeader() {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-[#EDF0F7] px-5 py-4">
      <div className="flex items-center gap-3">
        <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-[#F0EEFF] text-[#5145FF]">✦</span>
        <h2 className="text-[20px] font-bold text-[#172033]">AI 换粮建议</h2>
      </div>
      <p className="text-[13px] text-[#7A8499]">基于两款粮的营养得分、原料结构及风险标签，结合猫咪不同健康状态给出分场景建议。</p>
    </div>
  );
}

function ScenarioAdviceCard({ scenario, index }: { scenario: ChangeFoodAdvice["scenarios"][number]; index: number }) {
  const tone = adviceToneStyles[scenario.tone];
  return (
    <article className="rounded-2xl border border-[#E6EAF3] bg-white p-4 shadow-[0_5px_18px_rgba(30,41,59,0.04)]">
      <div className="mb-4 flex items-start gap-3">
        <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[15px] font-bold text-white ${tone.dot}`}>{index + 1}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[18px] font-bold text-[#172033]">{scenario.title}</h3>
            <span className={`rounded-md px-2 py-1 text-[12px] font-semibold ${tone.tag}`}>{scenario.recommendationLabel}</span>
          </div>
          <p className="mt-1 text-[13px] leading-5 text-[#657087]">{scenario.subtitle}</p>
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-[0.95fr_1.15fr_0.95fr]">
        <section className="rounded-xl border border-[#E6EAF3] p-4">
          <h4 className="mb-3 text-[13px] font-bold text-[#253047]">匹配条件</h4>
          <div className="space-y-2">
            {scenario.matchConditions.map((condition) => (
              <div key={condition} className="flex items-start gap-2 text-[12px] leading-5 text-[#59657A]">
                <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] ${tone.check}`}>✓</span>
                <span>{condition}</span>
              </div>
            ))}
          </div>
        </section>

        <div className="space-y-3">
          <section className="rounded-xl border border-[#E6EAF3] p-4">
            <h4 className="mb-2 text-[13px] font-bold text-[#253047]">推荐理由</h4>
            <p className="text-[12px] leading-6 text-[#59657A]">{scenario.reasonSummary}</p>
            <div className="mt-3 space-y-2">
              {scenario.reasonEvidence.map((evidence) => (
                <div key={evidence.label} className="grid grid-cols-[92px_1fr] items-center text-[12px]">
                  <span className={`font-semibold ${tone.accent}`}>→　{evidence.label}</span>
                  <span className="font-medium text-[#35415A]">{evidence.currentValue.toFixed(1)}　→　{evidence.targetValue.toFixed(1)}</span>
                </div>
              ))}
            </div>
          </section>
          {scenario.extraReason && (
            <section className="rounded-xl border border-[#E5E9F5] bg-[#F8FAFD] p-4">
              <h4 className="mb-2 text-[13px] font-bold text-[#35415A]">谨慎原因</h4>
              <p className="text-[12px] leading-6 text-[#657087]">{scenario.extraReason}</p>
            </section>
          )}
        </div>

        <div className="space-y-3">
          <section className="rounded-xl border border-[#FFE1C5] bg-[#FFF8F1] p-4">
            <h4 className="mb-2 flex items-center gap-2 text-[13px] font-bold text-[#F36D00]"><span>●</span> 注意事项</h4>
            <p className="text-[12px] leading-6 text-[#7B5A43]">{scenario.caution}</p>
          </section>
          {scenario.warningSignal && (
            <section className="rounded-xl border border-[#FFD4D6] bg-[#FFF1F2] p-4">
              <h4 className="mb-2 flex items-center gap-2 text-[13px] font-bold text-[#E83C3F]"><span>♟</span> 警示信号</h4>
              <p className="text-[12px] leading-6 text-[#805356]">{scenario.warningSignal}</p>
            </section>
          )}
        </div>
      </div>
    </article>
  );
}

function TransitionPlanPanel({ steps }: { steps: ChangeFoodAdvice["transitionPlan"] }) {
  return (
    <section className="border-t border-[#E8E6FF] bg-gradient-to-r from-[#F7F6FF] to-[#F3F6FF] px-5 py-4">
      <div className="mb-3 flex flex-wrap items-center gap-4">
        <h3 className="flex items-center gap-2 text-[16px] font-bold text-[#4034E8]"><span>⌘</span> 换粮过渡建议</h3>
        <p className="text-[12px] text-[#6F7890]">建议用 7–10 天完成换粮，循序渐进，降低肠道应激反应。</p>
      </div>
      <div className="grid gap-3 md:grid-cols-4">
        {steps.map((step) => (
          <div key={step.period} className="rounded-xl border border-[#E3E3F8] bg-white px-4 py-3 text-center shadow-sm">
            <div className="text-[13px] font-bold text-[#4034E8]">{step.period}</div>
            <div className="mt-2 text-[12px] leading-5 text-[#59657A]">新粮　{step.newFoodPercent}%<br />旧粮　{step.oldFoodPercent}%</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function DisclaimerBar({ text }: { text: string }) {
  return <div className="border-t border-[#EDF0F7] px-5 py-3 text-[11px] text-[#8A94A8]">ⓘ　{text}</div>;
}

function ChangeFoodAdvicePanel({ advice }: { advice: ChangeFoodAdvice }) {
  return (
    <section className="overflow-hidden rounded-2xl border border-[#E1E5F0] bg-white shadow-[0_8px_24px_rgba(30,41,59,0.05)]">
      <AdviceHeader />
      <div className="space-y-3 p-4">
        {advice.scenarios.map((scenario, index) => <ScenarioAdviceCard key={scenario.id} scenario={scenario} index={index} />)}
      </div>
      <TransitionPlanPanel steps={advice.transitionPlan} />
      <DisclaimerBar text={advice.disclaimer} />
    </section>
  );
}

function ChangeFoodAdvicePlaceholder({ selectedCount }: { selectedCount: number }) {
  const ready = selectedCount >= 2;
  return (
    <section className="rounded-2xl border border-dashed border-[#DCE3F3] bg-white px-6 py-10 text-center shadow-[0_8px_24px_rgba(30,41,59,0.04)]">
      <div className="mx-auto flex h-20 w-20 items-center justify-center rounded-2xl bg-[#F0EEFF] text-[34px] text-[#5145FF]">✦</div>
      <h2 className="mt-5 text-[20px] font-bold text-[#172033]">AI 换粮建议</h2>
      <p className="mx-auto mt-3 max-w-[560px] text-[13px] leading-6 text-[#7A8499]">
        {ready
          ? "已选择两款猫粮，点击“对比一下”后会自动生成换粮建议、注意事项和 7–10 天过渡方案。"
          : "先选择当前粮和目标粮。完成对比后，这里会展示换粮建议、谨慎原因、警示信号和过渡计划。"}
      </p>
    </section>
  );
}

function ChangeFoodAdviceLoading() {
  return (
    <section className="rounded-2xl border border-[#E1E5F0] bg-white px-6 py-8 shadow-[0_8px_24px_rgba(30,41,59,0.05)]">
      <div className="flex items-center gap-4">
        <span className="flex h-12 w-12 animate-pulse items-center justify-center rounded-xl bg-[#F0EEFF] text-[#5145FF]">✦</span>
        <div>
          <h2 className="text-[18px] font-bold text-[#172033]">正在生成 AI 换粮建议</h2>
          <p className="mt-1 text-[13px] text-[#7A8499]">正在结合两款粮的营养得分、原料结构和风险标签生成建议。</p>
        </div>
      </div>
    </section>
  );
}

function CatFoodComparePage() {
  const [productOptions, setProductOptions] = useState<ProductOption[]>([]);
  const [productsLoading, setProductsLoading] = useState(true);
  const [productsError, setProductsError] = useState("");
  const [currentFood, setCurrentFood] = useState("");
  const [targetFood, setTargetFood] = useState("");
  const [brandQuery, setBrandQuery] = useState("渴望");
  const [productQuery, setProductQuery] = useState("");
  const [priceFilter, setPriceFilter] = useState("全部");
  const [originFilter, setOriginFilter] = useState("全部");
  const [carouselPage, setCarouselPage] = useState(0);
  const [task, setTask] = useState<CatFoodTask | null>(null);
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState("");
  const [summary, setSummary] = useState<ChangeFoodAdvice | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState("");
  const [addProductOpen, setAddProductOpen] = useState(false);
  const [lastCompareKey, setLastCompareKey] = useState("");
  const catProfile: CatProfile = {
    age: "3岁",
    historyIssues: ["无明显历史问题"],
    recentSymptoms: ["无明显异常"],
  };

  useEffect(() => {
    let cancelled = false;
    async function loadProducts() {
      setProductsLoading(true);
      setProductsError("");
      try {
        const response = await fetch("/api/cat-food-compare/product-options?limit=500");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "产品库加载失败");
        if (cancelled) return;
        const nextProducts = (data.items || []).filter((item: ProductOption) => item.compare_available);
        setProductOptions(nextProducts);
      } catch (error) {
        if (!cancelled) setProductsError(error instanceof Error ? error.message : "产品库加载失败");
      } finally {
        if (!cancelled) setProductsLoading(false);
      }
    }
    void loadProducts();
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedCurrentFood = useMemo(() => findProductOption(productOptions, currentFood), [currentFood, productOptions]);
  const selectedTargetFood = useMemo(() => findProductOption(productOptions, targetFood), [targetFood, productOptions]);
  const selectedProducts = [selectedCurrentFood, selectedTargetFood].filter((product): product is ProductOption => Boolean(product));
  const selectedCount = selectedProducts.length;
  const canCompare = Boolean(currentFood && targetFood && currentFood !== targetFood && selectedCurrentFood && selectedTargetFood);

  const filteredOptions = useMemo(() => {
    const brandText = brandQuery.trim().toLowerCase();
    const productText = productQuery.trim().toLowerCase();
    return productOptions
      .filter((option) => option.compare_available)
      .filter((option) => {
        if (brandText && ![option.brand, option.raw_brand, option.label].filter(Boolean).join(" ").toLowerCase().includes(brandText)) return false;
        if (
          productText &&
          ![option.product_name, option.raw_title, option.label, option.display_text]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(productText)
        ) return false;
        if (priceFilter !== "全部" && option.price_bucket !== priceFilter) return false;
        if (originFilter !== "全部" && option.origin_type !== originFilter) return false;
        return true;
      });
  }, [brandQuery, originFilter, priceFilter, productOptions, productQuery]);

  const carouselTotalPages = Math.max(1, Math.ceil(filteredOptions.length / PRODUCT_CAROUSEL_PAGE_SIZE));
  const safeCarouselPage = Math.min(carouselPage, carouselTotalPages - 1);
  const pagedOptions = filteredOptions.slice(
    safeCarouselPage * PRODUCT_CAROUSEL_PAGE_SIZE,
    safeCarouselPage * PRODUCT_CAROUSEL_PAGE_SIZE + PRODUCT_CAROUSEL_PAGE_SIZE,
  );
  const carouselProducts = pagedOptions.length
    ? pagedOptions.map((option, index) => optionToProduct(option, safeCarouselPage * PRODUCT_CAROUSEL_PAGE_SIZE + index, productOptionValue(option) === currentFood || productOptionValue(option) === targetFood))
    : products;

  useEffect(() => {
    setCarouselPage(0);
  }, [brandQuery, originFilter, priceFilter, productQuery]);

  useEffect(() => {
    if (carouselPage >= carouselTotalPages) {
      setCarouselPage(carouselTotalPages - 1);
    }
  }, [carouselPage, carouselTotalPages]);

  function taskPayload() {
    return {
      task_type: "cat_food_compare",
      current_food: selectedCurrentFood?.label || currentFood,
      target_food: selectedTargetFood?.label || targetFood,
      current_display_brand: selectedCurrentFood?.brand || "",
      current_display_name: selectedCurrentFood?.product_name || "",
      target_display_brand: selectedTargetFood?.brand || "",
      target_display_name: selectedTargetFood?.product_name || "",
      current_source_id: selectedCurrentFood?.score_source_id || "",
      target_source_id: selectedTargetFood?.score_source_id || "",
      current_formula_id: selectedCurrentFood?.formula_id || "",
      target_formula_id: selectedTargetFood?.formula_id || "",
      current_product_key: selectedCurrentFood?.product_key || "",
      target_product_key: selectedTargetFood?.product_key || "",
      cat_profile: catProfile,
    };
  }

  async function createTaskIfNeeded() {
    if (task?.id) return task;
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

  async function fetchTask(taskId: string) {
    const response = await fetch(`/api/cat-food/tasks/${taskId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "任务状态加载失败");
    setTask(data.task);
    return data.task as CatFoodTask;
  }

  async function waitForCompareTask(taskId: string) {
    for (let index = 0; index < 60; index += 1) {
      const nextTask = await fetchTask(taskId);
      if (nextTask.status === "failed") throw new Error(nextTask.error_message || "任务执行失败");
      if (nextTask.status === "success" && nextTask.result?.compare) return nextTask;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("任务仍在处理中，请稍后刷新。");
  }

  async function handleCompare() {
    if (!canCompare || compareLoading) return;
    setCompareLoading(true);
    setCompareError("");
    setSummary(null);
    setSummaryError("");
    try {
      const currentTask = await createTaskIfNeeded();
      const response = await fetch(`/api/cat-food/tasks/${currentTask.id}/compare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(taskPayload()),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "对比任务启动失败");
      setTask(data.task);
      const finishedTask = await waitForCompareTask(currentTask.id);
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
    const compareKey = `${currentFood}::${targetFood}`;
    setSummaryLoading(true);
    setSummaryError("");
    try {
      const currentTask = await createTaskIfNeeded();
      const response = await fetch(`/api/cat-food/tasks/${currentTask.id}/summary`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          llm_context: compareResult.llm_context,
          friendly_rows: compareResult.friendly_rows,
          cat_profile: catProfile,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "大模型总结生成失败");
      setSummary(data.advice || null);
      setLastCompareKey(compareKey);
    } catch (error) {
      setSummaryError(error instanceof Error ? error.message : "大模型总结生成失败");
    } finally {
      setSummaryLoading(false);
    }
  }

  function handleSelectProduct(product: Product) {
    if (!product.value) return;
    if (product.value === currentFood) {
      setCurrentFood("");
      resetCompareState();
      return;
    } else if (product.value === targetFood) {
      setTargetFood("");
      resetCompareState();
      return;
    } else if (!currentFood) {
      setCurrentFood(product.value);
    } else {
      setTargetFood(product.value);
    }
    resetCompareState();
  }

  function handleRemoveProduct(value: string) {
    if (value === currentFood) {
      setCurrentFood("");
    }
    if (value === targetFood) {
      setTargetFood("");
    }
    resetCompareState();
  }

  function resetCompareState() {
    setTask(null);
    setCompareResult(null);
    setSummary(null);
    setCompareError("");
    setLastCompareKey("");
  }

  useEffect(() => {
    if (!compareResult || summary || summaryLoading) return;
    const compareKey = `${currentFood}::${targetFood}`;
    if (!currentFood || !targetFood || compareKey === lastCompareKey) return;
    void handleGenerateSummary();
  }, [compareResult, currentFood, lastCompareKey, summary, summaryLoading, targetFood]);

  const resultProducts = compareResult
    ? [
        resultToProduct(compareResult.current_food, selectedCurrentFood, compareResult.friendly_rows, "current", 0),
        resultToProduct(compareResult.target_food, selectedTargetFood, compareResult.friendly_rows, "target", 1),
      ]
    : products.slice(0, 2);

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#F8FAFF]">
      <HeaderNav />
      <div className="pt-[72px]">
        <main className="min-w-0 px-8 py-6 max-[1599px]:px-6">
          <div className="mx-auto max-w-[1500px] space-y-4">
            <SearchFilterBar
              brandQuery={brandQuery}
              productQuery={productQuery}
              priceFilter={priceFilter}
              originFilter={originFilter}
              onBrandQueryChange={setBrandQuery}
              onProductQueryChange={setProductQuery}
              onPriceFilterChange={setPriceFilter}
              onOriginFilterChange={setOriginFilter}
            />
            <ProductCarousel
              products={carouselProducts}
              onSelect={handleSelectProduct}
              loading={productsLoading}
              error={productsError}
              canPrev={filteredOptions.length > 0 && safeCarouselPage > 0}
              canNext={filteredOptions.length > 0 && safeCarouselPage < carouselTotalPages - 1}
              page={safeCarouselPage}
              totalPages={carouselTotalPages}
              totalCount={filteredOptions.length}
              onPrev={() => setCarouselPage((page) => Math.max(0, page - 1))}
              onNext={() => setCarouselPage((page) => Math.min(carouselTotalPages - 1, page + 1))}
            />
            <SelectedCompareBar
              selectedCount={selectedCount}
              selectedProducts={selectedProducts}
              loading={compareLoading}
              summaryLoading={summaryLoading}
              canCompare={canCompare}
              hasResult={Boolean(compareResult)}
              onAddProduct={() => setAddProductOpen(true)}
              onRemoveProduct={handleRemoveProduct}
              onCompare={handleCompare}
              onSummary={handleGenerateSummary}
            />
            {compareError && <div className="rounded-xl bg-rose-50 px-4 py-3 text-[13px] font-medium text-[#FF4D4F]">{compareError}</div>}
            {compareResult ? (
              <>
                <CompareResultSection products={resultProducts} />
                <NutritionBurdenComparisonChart result={compareResult} />
              </>
            ) : (
              <ChangeFoodAdvicePlaceholder selectedCount={selectedCount} />
            )}
            {summaryError && <div className="rounded-xl bg-rose-50 px-4 py-3 text-[13px] font-medium text-[#FF4D4F]">{summaryError}</div>}
            {summaryLoading && <ChangeFoodAdviceLoading />}
            {summary && <ChangeFoodAdvicePanel advice={summary} />}
            <div className="pb-6 text-[12px] font-normal text-[#7A8499]">ⓘ　以上信息基于公开资料与营养标准整理，仅供参考，建议结合宠物实际情况与医生建议。</div>
          </div>
        </main>
      </div>
      <AddProductModal open={addProductOpen} onClose={() => setAddProductOpen(false)} />
    </div>
  );
}

export default CatFoodComparePage;
