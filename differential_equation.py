import numpy as np
from scipy.integrate import odeint
import matplotlib.pyplot as plt

#numpy: for numerical arrays and generating time points.
#odeint: SciPy’s integrator for ODEs (legacy but widely used).
#matplotlib: for plotting the solution.

 
# 1. Define the differential equation function: dy/dt = f(y, t)
def model(y, t):
    dydt = -2 * y
    return dydt

# 2. Specify the initial condition and time points
y0 = 1          # initial condition
t = np.linspace(0, 2) # time points to solve for
#y0 sets the starting value.
#np.linspace(0, 2) creates 50 evenly spaced time points between 0 and 2 (the default num=50).


# 3. Solve the ODE
y = odeint(model, y0, t)

#odeint computes the numerical solution y(t)y(t)y(t) at each time in t.
#Shape note: odeint returns a 2D array of shape (len(t), 1) for a single ODE.




# 4. Plot the results
plt.plot(t, y)
plt.xlabel('time')
plt.ylabel('y(t)')
plt.title('Solution to dy/dt = -2y')

#Save the figure as a PNG file
plt.savefig('solution_plot.png', dpi=300)  # High resolution 



