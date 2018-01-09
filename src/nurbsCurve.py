import numpy as np

class NurbsCurve(object):
    '''
    https://fr.wikipedia.org/wiki/NURBS#Les_courbes_NURBS
    https://en.wikipedia.org/wiki/De_Boor%27s_algorithm#Local_support
    
    a = NurbsCurve(points=([10,10,0], [5,10,2], [-5,5,0], [10,5,-2], [4,10,0], [4,5,2], [8,1,0]), knots=[0,0,0,0,1,2,3,4,4,4,4], degree=3)
    a.draw_crv()
    param = .5
    pt_at_param = a.pt_at_param(param)
    tan_at_param = a.tan_at_param(param)
    a.compute_crv()
    a.draw_crv()
    '''
    def __init__(self, points, knots, degree, weights=None, LOD=20):
        '''
        :param  points: array of position of each 3d point (Control Point)
        :type   points: array(float3)
        :param   knots: knot vector (of length number  of CVs + degree + 1)
        :type    knots: array(float)
        :param  degree: degree of the curve
        :type   degree: int
        :param weights: array of int representing the weights of each CV
        :type  weights: array(int)
        :param     LOD: Level of detail - if we compute the curve, we'll compute
                        LOD points on the curve. With LOD=20, our full curve 
                        will have 20 out points. Useful only if we want to draw
                        the curve
        :type      LOD: int
        '''
        self._cvs = np.asarray(points)
        self._num_cvs = len(self._cvs)
        self._degree = degree
        self._order = self._degree + 1
        self._num_knots = self._num_cvs + self._degree + 1
        self._LOD = LOD
        self._knots = knots
        self._out_pts = None  # the curve hasn't been computed yet
        self._weights = weights or np.ones(self._num_cvs)

    def compute_crv(self):
        ''' 
        Computes the curve at n parameters (with n=LOD). Running this will 
        populate _out_pts, that we can use to draw the curve if needed
        '''
        self._out_pts = []
        for i in xrange(self._LOD):
            t = self._knots[self._num_knots-1] * i / (self._LOD-1.)
            if i == self._LOD-1:
                t -= .0001

            out_point   = self.pt_at_param(t)
            # out_tangent = self.tan_at_param(t)

            self._out_pts.append(out_point)

        return self._out_pts

    def _CoxDeBoor(self, t, i, k, knots):
        '''
        Recursive function to find the value N affecting the current parameter
        :param t: parameter
        :type  t: float
        :param i: index of the CV we treat currently
        :type  i: int
        :param k: order (i.e. degree + 1 by default, on which we removes 1 
                  until k = 1). The order is the parameter to change to have 
                  an open / close / periodic curve
        :type  k: int
        :param knots: knot vector.
        :type  knots: list of int
        :return: 
        '''
        if k == 1:
            if knots[i] <= t and t <= knots[i+1]:
                return 1.
            else:
                return 0.
        denominator1 = knots[i+k-1] - knots[i]
        denominator2 = knots[i+k] - knots[i+1]
        Eq1 = 0
        Eq2 = 0    
        if denominator1 > 0:
            Eq1 = ((t-knots[i]) / denominator1) * self._CoxDeBoor(t, i, k-1, knots)
        if denominator2 > 0:
            Eq2 = (knots[i+k]-t) / denominator2 * self._CoxDeBoor(t, i+1, k-1, knots)
    
        return Eq1 + Eq2

    def pt_at_param(self, t):
        '''
        Returns the float3 position of a point at the given parameter t
        :param t: parameter we query
        :type  t: float
        :return     : position of the point in 3D
        :return type: np.array()
        '''
        # sum of the effect of all the CVs on the curve at the given parameter 
        # to get the evaluated curve point
        numerator = np.zeros(3)
        denominator = 0
        for i in xrange(self._num_cvs):
            # compute the effect of this point on the curve
            N = self._CoxDeBoor(t, i, self._order, self._knots)
            if N > .0001:
                numerator += (self._weights[i] * self._cvs[i] * N)
                denominator += (self._weights[i] * N)

        return numerator / denominator

        '''
        # If we don't need to have weighted values, we can use this equation
        # to make thigns faster
        for i in xrange(self._num_cvs):
            val = self._CoxDeBoor(t, i, self._order, self._knots)
            if val > .001:
                # sum effect of CV on this part of the curve
                out_pt[0] += val * self._cvs[i][0]
                out_pt[1] += val * self._cvs[i][1]
                out_pt[2] += val * self._cvs[i][2]
        '''

    def tan_at_param(self, t):
        '''
        Returns the vector of the tangent at the given parameter t
        :param t: parameter we query
        :type  t: float
        :return     : position of the point in 3D
        :return type: np.array()
        '''
        numerator1A   = np.zeros(3)
        numerator1B   = np.zeros(3)
        numerator2A   = np.zeros(3)
        numerator2B   = np.zeros(3)
        denominator   = 0

        for i in xrange(self._num_cvs):
            N = self._CoxDeBoor(t, i, self._order, self._knots)
            dN = self._CoxDeBoorDerived(t, i, self._order, self._knots)

            if N > .0001:
                denominator += (self._weights[i] * N)
                # first equation
                numerator1A += (self._weights[i] * self._cvs[i] * dN)
                numerator1B += (self._weights[i] * N)
                # second equation
                numerator2A += (self._weights[i] * self._cvs[i] * N)
                numerator2B += (self._weights[i] * dN)

        eq1 = (numerator1A * numerator1B) / (denominator**2)
        eq2 = (numerator2A * numerator2B) / (denominator**2)

        return eq1 - eq2

    def _CoxDeBoorDerived(self, t, i, k, knots):
        ''' 
        Derivated function of the _CoxDeBoor algorithm, used only for the tangent
        :param t: parameter
        :type  t: float
        :param i: index of the CV we treat currently
        :type  i: int
        :param k: order (i.e. degree + 1 by default, on which we removes 1 
                  until k = 1). The order is the parameter to change to have 
                  an open / close / periodic curve
        :type  k: int
        :param knots: knot vector.
        :type  knots: list of int
        :return: 
        '''
        if k == 1:
            if knots[i] <= t and t <= knots[i+1]:
                return 1.
            else:
                return 0.
    
        denominator1 = knots[i+k-1] - knots[i]
        denominator2 = knots[i+k] - knots[i+1]
        Eq1 = 0
        Eq2 = 0    

        if denominator1 > 0:
            Eq1 = (k / denominator1) * self._CoxDeBoor(t, i, k-1, knots)
        if denominator2 > 0:
            Eq2 = (k / denominator2) * self._CoxDeBoor(t, i+1, k-1, knots)

        return Eq1 - Eq2

    def draw_crv(self):
        ''' 
        Convenient function to compare our result with the same parameters 
        using maya curve
        '''
        cmds.curve(n='mayaCrv', d=self._degree, p=self._cvs, k=self._knots[1:-1])
        cmds.curve(n='myCrv', d=1, p=self._out_pts)
