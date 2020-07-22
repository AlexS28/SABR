"""FILE CREATED BY: Alexander Schperberg, aschperb@gmail.com
Copyright by RoMeLa (Robotics and Mechanisms Laboratory, University of California, Los Angeles)"""

# This file provides a stochastic and robust model predictive controller for a simple unmanned ground vehicle that
# moves a ground vehicle to any desired goal location, while considering obstacles (represented as 2D polygons) and
# cross communication ability with another robot


from casadi import *
import numpy as np



class SMPC_UGV_Planner():

    def __init__(self, dT, mpc_horizon, curr_pos, goal_pos, robot_size, max_nObstacles, field_of_view, lb_state,
                 ub_state, lb_control, ub_control, Q, R, P, G, angle_noise_r1, angle_noise_r2,
                 relative_measurement_noise_cov, maxComm_distance):

        # initialize Optistack class
        self.opti = casadi.Opti()

        # dt = discretized time difference
        self.dT = dT
        # mpc_horizon = number of time steps for the mpc to look ahead
        self.N = mpc_horizon
        # robot_size = input a radius value, where the corresponding circle represents the size of the robot
        self.robot_size = robot_size
        # max_nObstacles = total number of obstacles the mpc constraints are allowed to use in its calculations
        self.max_nObstacles = max_nObstacles
        # view_distance = how far and wide the robot's sensor is allowed to see its surrounding obstacles,
        # an example input is the following: field_of_view = {'max_distance': 10.0, 'angle_range': [45, 135]}
        self.field_of_view = field_of_view
        # lower_bound_state = numpy array corresponding to the lower limit of the robot states, e.g.
        # lb_state = np.array([[-20], [-20], [-pi], dtype=float), the same for the upper limit (ub). Similar symbolic
        # representation for the controls (lb_control and ub_control) as well
        self.lb_state = lb_state
        self.ub_state = ub_state
        self.lb_control = lb_control
        self.ub_control = ub_control
        # Q and R diagonal matrices, used for the MPC objective function, Q is 3x3, R is 4x4 (first 2 diagonals
        # represent the cost on linear and angular velocity, the next 2 diagonals represent cost on state slack,
        # and terminal slack respectively. The P diagonal matrix represents the cost on the terminal constraint.
        self.Q = Q
        self.R = R
        self.P = P
        self.G = G
        # initialize discretized state matrices A and B (note, A is constant, but B will change as it is a function of
        # state theta)
        self.A = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        self.B = np.array([[self.dT, 0, 0], [0, self.dT, 0], [0, 0, self.dT]])
        # initialize measurement noise (in our calculation, measurement noise is set by the user and is gaussian,
        # zero-mean). It largely represents the noise due to communication transmitters, or other sensor devices. It
        # is assumed to be a 3x3 matrix (x, y, and theta) for both robots
        self.relative_measurement_noise_cov = relative_measurement_noise_cov
        # we assume that there is constant noise in angle (while x and y are dynamically updated) - should be a variance
        # value
        self.angle_noise_r1 = angle_noise_r1
        self.angle_noise_r2 = angle_noise_r2
        # initialize the maximum distance that robot 1 and 2 are allowed to have for cross communication
        self.maxComm_distance = maxComm_distance
        # initialize robot's current position
        self.curr_pos = curr_pos
        # initialize robot's goal position
        self.goal_pos = goal_pos
        # initialize cross diagonal system noise covariance matrix
        self.P12 = np.array([[0,0,0], [0,0,0], [0,0,0]])
        # bool variable to indicate whether the robot has made first contact with the uav
        self.first_contact = False
        # initialize state, control, and slack variables
        self.initVariables()
        # initialize objective function
        self.obj()

    def initVariables(self):

        # initialize x, y, and theta state variables
        self.X = self.opti.variable(3, self.N+1)
        self.x_pos = self.X[0,:]
        self.y_pos = self.X[1,:]
        self.th = self.X[2, :]
        self.opti.set_initial(self.X, 0)


        # initialize, linear and angular velocity control variables (v, and w), and repeat above procedure
        self.U = self.opti.variable(3, self.N)
        self.vx = self.U[0,:]
        self.vy = self.U[1, :]
        self.w = self.U[2,:]
        self.opti.set_initial(self.U, 0)

        # initialize slack variables for states for prediction horizon N
        self.S1 = self.opti.variable(2, self.N)
        # initialize slack variable for the terminal state, N+1
        self.ST = self.opti.variable(2, 1)

        # initialize the current robot pos (x, y and th current position)
        self.r1_pos = self.opti.parameter(3, 1)
        self.opti.set_value(self.r1_pos, self.curr_pos)

        # initialize the goal robot pos (x, y, and th goal position)
        self.r1_goal = self.opti.parameter(3, 1)
        self.opti.set_value(self.r1_goal, self.goal_pos)

        # initialize the uncertainty covariances from the RNN, provided by robot 1 (4 x 1 vector per covariance matrix)
        # must be positive semi-definite
        self.r1_pos_cov = self.opti.parameter(4, self.N+1)
        self.opti.set_value(self.r1_pos_cov, 0)

        # initialize robot 2, future positions (x, y, and th)
        self.r2_traj = self.opti.parameter(3, self.N+1)
        self.opti.set_value(self.r2_traj, 0)

        # initialize the uncertainty covariances from the RNN, provided by robot 2 (4 x 1 vector per covariance matrix)
        # must be positive semi-definite
        self.r2_pos_cov = self.opti.parameter(4, self.N+1)
        self.opti.set_value(self.r2_pos_cov, 0)

    # objective function, with consideration of a final terminal state
    def obj(self):
        self.objFunc = 0
        ref_st = self.r1_goal

        for k in range(0, self.N-1):

            con = self.U[:, k]
            st = self.X[:, k+1]

            self.objFunc = self.objFunc + mtimes(mtimes((st - ref_st).T, self.Q), st - ref_st) + mtimes(
                mtimes(con.T, self.R), con)

        # add the terminal state to the objective function
        st = self.X[:, self.N]
        con = self.U[:, self.N-1]
        self.objFunc = self.objFunc + mtimes(mtimes((st - ref_st).T, self.P), st - ref_st) + mtimes(
            mtimes(con.T, self.G), con)

        # initialize the constraints for the objective function
        self.init_constraints()

        # initialize the objective function into the solver
        self.opti.minimize(self.objFunc)

        # options for solver
        opts = {'ipopt': {'max_iter': 100, 'print_level': False, 'acceptable_tol': 10**-5,
                          'acceptable_obj_change_tol': 10**-5}}

        opts.update({'print_time': 0})

        # create the solver
        self.opti.solver('ipopt', opts)


    # the nominal next state is calculated for use as a terminal constraint in the objective function
    def next_state_nominal(self, x, u):
        next_state = mtimes(self.A, x) + mtimes(self.B, u)
        return next_state

    # the next state is calculated with consideration of system noise, also considered the true state
    def next_state_withSystemNoise(self, x, u, system_noise_cov):
        # the system_noise_covariance will be a flattened 1x4 array, provided by the output of an RNN. We need to
        # convert it into a 3x3 matrix. We will assume a constant noise in theta however.

        system_noise_cov_converted = np.array([[self.opti.value(system_noise_cov[0]),
                self.opti.value(system_noise_cov[1])], [self.opti.value(system_noise_cov[2]),
                                                        self.opti.value(system_noise_cov[3])]])


        # sample a gaussian distribution of the system_noise covariance (for x and y)
        system_noise_xy = np.random.multivariate_normal([0,0], system_noise_cov_converted, check_valid='warn').reshape(2,1)
        # sample a gaussian distribution of theta
        #system_noise_th = np.sqrt(angle_noise_r1)
        system_noise_th = np.random.normal(0, angle_noise_r1)

        system_noise = np.append(system_noise_xy, system_noise_th)
        print(system_noise)

        # add the noise to the linear and angular velocity

        self.B = np.array([[self.dT, 0, 0], [0, self.dT, 0], [0, 0, self.dT]])
        next_state = mtimes(self.A, x) + mtimes(self.B, u) + system_noise
        return next_state

    # initialize all required constraints for the objective function
    def init_constraints(self):
        # constrain the current state
        self.opti.subject_to(self.X[:,0] == self.r1_pos)

        # provide inequality constraints or bounds on the state and control variables, with the additional slack variable
        #self.opti.subject_to(self.opti.bounded(self.lb_state[0:2]-self.S1,
        #                                       self.X[0:2, self.N], self.ub_state[0:2] + self.S1))
        self.opti.subject_to(self.opti.bounded(self.lb_state, self.X, self.ub_state))
        self.opti.subject_to(self.opti.bounded(self.lb_control, self.U, self.ub_control))

        for k in range(0, self.N-1):

            next_state = if_else(sqrt((self.X[0, k] - self.r2_traj[0, k])**2 +
                                      (self.X[1, k] - self.r2_traj[1, k]**2)) > self.maxComm_distance,
                                 self.update_1(self.X[:,k], self.U[:,k], k), 0)

            self.opti.subject_to(self.X[:,k+1] == next_state)

        # include the terminal constraint
        next_state = self.next_state_nominal(self.X[:, self.N-1], self.U[:, self.N-1])
        self.opti.subject_to(self.X[:, self.N] == next_state)

    def update_1(self, x, u, k):
        system_noise_cov = self.r1_pos_cov[:, k]
        return self.next_state_withSystemNoise(x, u, system_noise_cov)


    def update_2(self, x, u):
        return self.next_state_nominal(x, u)

    def update_3(self):
        pass


    def pre_solve(self):
        pass


if __name__ == '__main__':
    # initialize all required variables for the SMPC solver
    dT = 0.1
    mpc_horizon = 10
    curr_pos = np.array([0,0,0]).reshape(3,1)
    goal_pos = np.array([10,0,0.5]).reshape(3,1)
    robot_size = 0.1
    max_nObstacles = 10
    field_of_view = {'max_distance': 10.0, 'angle_range': [45, 135]}
    lb_state = np.array([[-20], [-20], [-2*pi]], dtype=float)
    ub_state = np.array([[20], [20], [2*pi]], dtype=float)
    lb_control = np.array([[-0.5], [-0.5], [-0.1]], dtype=float)
    ub_control = np.array([[0.5], [0.5], [0.1]], dtype=float)
    Q = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    R = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.05]])
    P = np.array([[10, 0, 0], [0, 10, 0], [0, 0, 1000]])
    G = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 10]])
    angle_noise_r1 = 0.0
    angle_noise_r2 = 0.0
    relative_measurement_noise_cov = np.array([[0,0,0], [0,0,0], [0,0,0]])
    maxComm_distance = -10

    SMPC = SMPC_UGV_Planner(dT, mpc_horizon, curr_pos, goal_pos, robot_size, max_nObstacles, field_of_view, lb_state,
                            ub_state, lb_control, ub_control, Q, R, P, G, angle_noise_r1, angle_noise_r2,
                            relative_measurement_noise_cov, maxComm_distance)


    while numpy.linalg.norm(curr_pos - goal_pos) > 0.01:

        sol = SMPC.opti.solve()
        x = sol.value(SMPC.X)[:,1]
        curr_pos = np.array(x).reshape(3,1)
        SMPC.opti.set_value(SMPC.r1_pos, x)








"""
        # inputs are the position of robot 1, robot 2, and their corresponding system noise covariance matrix provided
        # by the RNN

        # the output will be the updated state and also the updated system noise covariance matrix

        # check if this is the first time both robots are making contact, and if they are within distance
        if self.first_contact and np.linalg.norm(x_r1[0:2] - x_r2[0:2]) < self.maxComm_distance:

            # calculate the measured state before update for both robots
            # TODO use the correct x_hat for both robots
            x_hat_r1 = 0
            x_hat_r2 = 0

            # calculate the relative positions of the two contacting robots
            z = x_r1 - x_r2

            # the system_noise_covariance will be a flattened 1x4 array, provided by the output of an RNN. We need to
            # convert it into a 3x3 matrix. We will assume a constant noise in theta however.
            #TODO Check to see if we need to include the off-diagonal values of the system noise covariance

            system_noise_cov_converted_r1 = np.array([[system_noise_cov_r1[0], 0, 0], [0, system_noise_cov_r1[3], 0],
            [0, 0, self.angle_noise_r1]])

            system_noise_cov_converted_r2 = np.array([[system_noise_cov_r2[0], 0, 0], [0, system_noise_cov_r2[3], 0],
            [0, 0, self.angle_noise_r2]])

            P11 = system_noise_cov_converted_r1
            P22 = system_noise_cov_converted_r2

            # placeholder R12 (relative measurement noise between robot 1 and 2)
            R12 = np.zeros(3,3)             #TODO: Add the correct R12 matrix here

            # calculate the S matrix
            S = P11 + P22 + R12

            # calculate the inverse S matrix, if not possible, assume zeros
            try:
                S_inv = np.linalg.inv(S)

            except ValueError:
                S_inv = np.zeros(3, 3)

            # update the cross-diagonal matrix
            self.P12 = mtimes(mtimes(P11 * S_inv) * P22)

            # calculate the kalman gain
            K = mtimes(P11, S_inv)

            # calculate the updated state for the ugv
            x_hat_r1 = x_hat_r1 + mtimes(K, z - (x_hat_r1 - x_hat_r2))

            # calculate the updated system noise covariance for the ugv
            P11 = P11 - mtimes(mtimes(P11, S_inv), P11)

            # ensure this function is only run at first contact
            self.first_contact = False

            return x_hat_r1, P11

        # the second update and beyond, the following equations are used if both robots are within contact
        elif (not self.first_contact) and np.linalg.norm(x_r1[0:2] - x_r2[0:2]) < self.maxComm_distance:

            # calculate the measured state before update for both robots
            # TODO use the correct x_hat for both robots
            x_hat_r1 = 0
            x_hat_r2 = 0

            # calculate the relative positions of the two contacting robots
            z = x_r1 - x_r2

            # the system_noise_covariance will be a flattened 1x4 array, provided by the output of an RNN. We need to
            # convert it into a 3x3 matrix. We will assume a constant noise in theta however.
            # TODO Check to see if we need to include the off-diagonal values of the system noise covariance

            system_noise_cov_converted_r1 = np.array([[system_noise_cov_r1[0], 0, 0], [0, system_noise_cov_r1[3], 0],
            [0, 0, self.angle_noise_r1]])

            system_noise_cov_converted_r2 = np.array([[system_noise_cov_r2[0], 0, 0], [0, system_noise_cov_r2[3], 0],
            [0, 0, self.angle_noise_r2]])

            P11_before_upd = system_noise_cov_converted_r1
            P22_before_upd = system_noise_cov_converted_r2

            # placeholder R12 (relative measurement noise between robot 1 and 2)
            R12 = np.zeros(3,3)             #TODO: Add the correct R12 matrix here

            # calculate the S matrix
            S = P11_before_upd - self.P12 - np.transpose(self.P12) + P22_before_upd + R12

            # calculate the inverse S matrix, if not possible, assume zeros
            try:
                S_inv = np.linalg.inv(S)

            except ValueError:
                S_inv = np.zeros(3, 3)

            # calculate the kalman gain
            K = mtimes((P11_before_upd - self.P12), S_inv)

            # calculate the updated state for the ugv
            x_hat_r1 = x_hat_r1 + mtimes(K, z - (x_hat_r1 - x_hat_r2))


            # calculate the updated system noise covariance for the ugv
            P11 = P11_before_upd - mtimes(mtimes((P11_before_upd - self.P12), S_inv), (P11_before_upd - self.P12))

            # update the cross-diagonal matrix
            # TODO double check the bottom and top equations, could be incorrect
            self.P12 = self.P12 - mtimes(mtimes((P11_before_upd - self.P12), S_inv), (self.P12 - P22_before_upd))

            return x_hat_r1, P11

        # if the robots are not in contact range, then no cooperative localization calculations can be done
        else:
            # calculate the measured state before update for both robots
            x_hat_r1 = x_r1 + numpy.random.multivariate_normal(0, self.measurement_noise_cov_r1, check_valid='warn')

            # the system_noise_covariance will be a flattened 1x4 array, provided by the output of an RNN. We need to
            # convert it into a 3x3 matrix. We will assume a constant noise in theta however.
            # TODO Check to see if we need to include the off-diagonal values of the system noise covariance
            system_noise_cov_converted_r1 = np.array([[system_noise_cov_r1[0], 0, 0], [0, system_noise_cov_r1[3], 0],
                                                      [0, 0, self.angle_noise_r1]])
            P11 = system_noise_cov_converted_r1


            return x_hat_r1, P11

    def obj(self):
        pass
"""