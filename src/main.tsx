import React from "react";
import { createRoot } from "react-dom/client";

import CatFoodComparePage from "../vendor/csv_mysql_labeling/config/CatFoodComparePage";
import "./styles.css";

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <CatFoodComparePage />
  </React.StrictMode>
);
