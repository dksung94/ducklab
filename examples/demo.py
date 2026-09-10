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
