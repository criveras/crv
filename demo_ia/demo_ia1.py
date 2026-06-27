from xgboost import XGBRegressor
import numpy as np

X = np.random.rand(100,3)
y = np.random.rand(100)

model = XGBRegressor()
model.fit(X,y)

print("Modelo entrenado OK")
