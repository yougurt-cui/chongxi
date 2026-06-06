import React from "react";
import { createRoot } from "react-dom/client";

import CatFoodComparePage from "../vendor/csv_mysql_labeling/config/CatFoodComparePage";
import RecommendationEnginePage from "./RecommendationEnginePage";
import "./styles.css";

function App() {
  if (window.location.pathname.startsWith("/consumer/recommendation-engine")) {
    return <RecommendationEnginePage />;
  }
  return <CatFoodComparePage />;
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
