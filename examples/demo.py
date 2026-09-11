# %% hello
print("hello from ducklab")
x = 21

# %% compute
y = x * 2
y

# %% plot
import matplotlib.pyplot as plt
import numpy as np
t = np.linspace(0, 4 * np.pi, 300)
plt.figure(figsize=(6, 2.5))
plt.plot(t, np.sin(t) * np.exp(-t / 8))
plt.title("damped sine")
plt.tight_layout()
plt.show()

# %% error demo
1 / 0

# %% bar chart
import matplotlib.pyplot as plt
import numpy as np

rng = np.random.default_rng(0)
fruits = ["apple", "banana", "cherry", "grape", "mango"]
sales = rng.integers(20, 100, size=len(fruits))

plt.figure(figsize=(6, 3))
bars = plt.bar(fruits, sales, color="#4C78A8")
plt.bar_label(bars)
plt.title("weekly fruit sales")
plt.ylabel("units")
plt.tight_layout()
plt.show()

# %% sales summary
# uses `fruits` and `sales` from the bar chart cell (kernel state persists)
ranked = sorted(zip(fruits, sales), key=lambda p: p[1], reverse=True)
for name, n in ranked:
    print(f"{name:<8} {n:>4}  {'#' * (n // 5)}")
print(f"\ntotal {sales.sum()}, top seller: {ranked[0][0]}")

# %% pie chart
# share of weekly sales per fruit (reuses `fruits` and `sales`)
plt.figure(figsize=(4.5, 4.5))
plt.pie(sales, labels=fruits, autopct="%1.0f%%", startangle=90,
        colors=["#4C78A8", "#F58518", "#E45756", "#72B7B2", "#54A24B"])
plt.title("weekly fruit sales share")
plt.tight_layout()
plt.show()
# %% ㅁㄴㅇㄹㄴ
a = 1

# %%

