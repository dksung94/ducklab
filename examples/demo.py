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
