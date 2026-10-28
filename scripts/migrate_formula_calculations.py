"""Run once before enabling CHONGXI_VERSIONED_CALCULATION=1."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.formula_calculation_service import migrate
if __name__ == '__main__':
    from services.catfood_standardization_service import init_standardization_db
    init_standardization_db()
    migrate()
    print('Formula calculation schema ready.')
