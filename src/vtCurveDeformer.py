import maya.OpenMaya       as om
import maya.OpenMayaMPx    as omMpx
import maya.OpenMayaAnim   as omAnim
import maya.OpenMayaRender as OpenMayaRender
import maya.OpenMayaUI     as OpenMayaUI
import sys
import numpy as np
from math import sqrt
from random import randint

#sys.path.insert(0, '/Users/fruity/Documents/_dev/fToolbox/vtPlugins/vtCurveDeformer/src/')
import nurbsCurve;reload(nurbsCurve)

pluginName = 'curveDeformer'
pluginId = om.MTypeId(0x1272C9)
glRenderer = OpenMayaRender.MHardwareRenderer.theRenderer()
glFT = glRenderer.glFunctionTable()


np.set_printoptions(precision=3)

'''
cmds.delete(df)
cmds.flushUndo()
cmds.unloadPlugin('vtCurveDeformer')
cmds.file('/Users/fruity/Documents/_dev/curveDeformer/scenes/example_scene_2015.ma', o=1, f=1)
cmds.loadPlugin('/Users/fruity/Documents/_dev/fToolbox/vtPlugins/vtCurveDeformer/src/vtCurveDeformer.py')
df = cmds.deformer('pCylinder1', type='curveDeformer')[0]
cmds.connectAttr('inCrv.worldSpace', df + '.inCrv')
cmds.connectAttr('baseCrv.worldSpace', df + '.baseCrv')
cmds.connectAttr('P.worldMatrix', df + '.matrixJoints[0].matrixJoint')
cmds.connectAttr('O.worldMatrix', df + '.matrixJoints[1].matrixJoint')
cmds.connectAttr('Q.worldMatrix', df + '.matrixJoints[2].matrixJoint')
cmds.refresh()
cmds.setAttr(df + '.initialize', False)
'''

class curveDeformer(omMpx.MPxDeformerNode):
    '''
    From what I understood, we have roughly 5 steps:
    - build a matrix for each CV. Pos is the pos of the CV, and rotations are
      computed using joint orientations (if both jointA and jointB has a weight 
      of .5, the matrix of the CV will have an orientation matrix based on 
      jointA/jointB slerp-ed at .5)
    - for each vertex, offset each CV of the inCrv by delta, which is the 
      vector [closest point from the vtx on the curve -> vtx]. Each CV being 
      offseted, we have a new -offset- curve passing through the vertex
    - moves individually each CV of the offset curves, based on :
        1 - the angle of the bones (in the paper, shoulder = P, elbow = O, 
        wrist = Q, and the current vertex = R). This multiplier, 
        computed in Eq. 11, is known as Tau
        2 - another multiplier, based on the distance from the vertex to the 
        mid joint (i.e. O). This is, in the MOS paper (dreamworks), detailled 
        as the "inverse distance weighting" strategy
    - knowing the direction in which we need to push (or pull) the CV, we 
      multiply this direction by Tau and the CV weight
    - finally, we get the point at parameter for the current vtx, on the fix crv
    '''
    aInit      = om.MObject()
    aInCrv     = om.MObject()
    aBaseCrv   = om.MObject()
    aWeight    = om.MObject()
    aCps       = om.MObject()
    aMatrixJoint  = om.MObject()
    aMatrixJoints = om.MObject()

    if om.MGlobal.apiVersion() < 201600:
        _input = omMpx.cvar.MPxDeformerNode_input # note: input is a python builtin
        inputGeom = omMpx.cvar.MPxDeformerNode_inputGeom
        envelope = omMpx.cvar.MPxDeformerNode_envelope
        outputGeom = omMpx.cvar.MPxDeformerNode_outputGeom
    else:
        _input = omMpx.cvar.MPxGeometryFilter_input # note: input is a python builtin
        inputGeom = omMpx.cvar.MPxGeometryFilter_inputGeom
        envelope = omMpx.cvar.MPxGeometryFilter_envelope
        outputGeom = omMpx.cvar.MPxGeometryFilter_outputGeom
    
    def __init__(self):
        omMpx.MPxDeformerNode.__init__(self)
   
    def deform(self, data, itGeo, localToWorldMatrix, geomIndex):
        # 
        # get input datas
        hDeformedMeshArray = data.outputArrayValue(curveDeformer._input)
        hDeformedMeshArray.jumpToElement(geomIndex)
        hDeformedMeshElement = hDeformedMeshArray.outputValue()
        oDeformedMesh = hDeformedMeshElement.child(curveDeformer.inputGeom).asMesh()
        fnDeformedMesh = om.MFnMesh(oDeformedMesh)
        
        # ----------------------------------------------------------------------
        #                               GET THE ATTRIBUTES
        # ---------------------------------------------------------------------- 
        # get the init state
        initialize = data.inputValue(self.aInit).asBool()

        # get the in curve
        oCrv = data.inputValue(curveDeformer.aInCrv).asNurbsCurve()
        if oCrv.isNull(): return
        fnCrv = om.MFnNurbsCurve(oCrv)

        # get the base curve
        oBaseCrv = data.inputValue(self.aBaseCrv).asNurbsCurve()
        if oBaseCrv.isNull(): return
        fnBaseCrv = om.MFnNurbsCurve(oBaseCrv)

        # get general curve infos
        degree = fnCrv.degree()
        dummy = om.MDoubleArray()
        fnCrv.getKnots(dummy)
        knots = [dummy[i] for i in xrange(dummy.length())]
        knots = [knots[0]] + knots + [knots[-1]]

        # envelope
        envelopeHandle = data.inputValue(curveDeformer.envelope)
        env = envelopeHandle.asFloat()
        if not env: return

        # get the matrix of each joint (to compute Tau)
        self.jts_pos = []
        hMatrixJointsArray = data.inputArrayValue(self.aMatrixJoints)
        if hMatrixJointsArray.elementCount() < 3:
            return  # we need at least 3 joints to compute Tau
        for i in xrange(hMatrixJointsArray.elementCount()):
            hMatrixJointsArray.jumpToArrayElement(i)
            jt_mat = om.MTransformationMatrix(hMatrixJointsArray.inputValue().child(curveDeformer.aMatrixJoint).asMatrix())
            vPos = jt_mat.getTranslation(om.MSpace.kWorld)
            self.jts_pos.append([vPos.x, vPos.y, vPos.z])
        # get the control points and the weights
        weights = []  # list of floats
        hCvArray = data.inputArrayValue(self.aCps)

        for i in xrange(hCvArray.elementCount()):
            hCvArray.jumpToArrayElement(i)
            fWeight = hCvArray.inputValue().child(curveDeformer.aWeight).asFloat()
            weights.append(fWeight)

        # ---------------------------------------------------------------------- 
        # get the CVs of both the base and normal curves
        cvs_array = om.MPointArray()
        cvs_base_array = om.MPointArray()
        fnCrv.getCVs(cvs_array)
        fnBaseCrv.getCVs(cvs_base_array)

        # make sure the weights array have a valid length (as many elements as there are CVs)
        num_cvs = cvs_array.length()
        if len(weights) < num_cvs:
            weights.extend([1] * (num_cvs - len(weights)))
        elif len(weights) > num_cvs:
            weights = weights[:num_cvs]

        # ----------------------------------------------------------------------
        #                               INITIALIZE
        # ---------------------------------------------------------------------- 
        # at init stage, we do : 
        # 1 - get the skinCluster of the curve
        # 2 - get the matrices / weights of the joints influencing the SC
        # 3 - get an average matrix for each CP
        # 4 - get the offset vector delta between closest point on curve 
        #     and current vertex
        # 5 - get the 3 closest joints for each vertex and compute Tau
        # 6 - assign a weight for each offset CV based on the distance with the vtx
        if initialize:
            # 1 - get the skinCluster attached to the curve and the dag path
            fnSc, dpInCrv = self.get_skin_cluster()
            # 2 - get the bones and weights
            self._weights = self.get_skin_weights(fnSc, dpInCrv)
            # 3 - compute the base transformation matrix for each CV
            self._dpJoints = om.MDagPathArray()
            fnSc.influenceObjects(self._dpJoints)
            self._base_mats_per_cv = self.get_mat_per_cv(self._dpJoints, cvs_base_array)
            # 4 - compute offset vector and parameter
            self._pOffsets, self._params = self.get_offsets_and_params(itGeo, fnBaseCrv)
            # 5 - get the 3 closest joints for each vertex, to compute Tau parameter later. This is a list of the 3 closest joint indices, for each vertex
            self._closest_jts_idx = self.get_3_closest_jts_per_vertex(itGeo, self.jts_pos)
            P, O, Q = self._closest_jts_idx[itGeo.index()]
            # 6 - assign a weight to each offset cv
            self._dist_CV_weights = self.inverse_distance_weighting(O, cvs_base_array)
            # 7 - set the direction of the offset CVs method (in deform)
            self._directions_mat  = self.set_offset_direction(itGeo, self._pOffsets, cvs_base_array, self._base_mats_per_cv)
            # 8 - get the Tau values by default to remap them efficiently later
            self._default_taus = self.get_default_taus(itGeo, P, O, Q)

        # ----------------------------------------------------------------------
        #                               DEFORM
        # ---------------------------------------------------------------------- 
        # to rebuild the curve, we need to get 2 things :
        # - the offset between the current vertex and the closest point 
        #   on curve computed in the initialize and stored in self._pOffsets
        # - the transformationMatrix between all the CVs of the base_crv and the crv
        # once we have that, we just add the offset to the transformMatrix to get the 
        # virtual cvs of the offset curve
        else:
            out_positions = om.MPointArray()
            while not itGeo.isDone():
                # compute the Tau multiplier
                P, O, Q = self._closest_jts_idx[itGeo.index()]
                offset_cvs = []
                weighted_matrices = []
                # self._np_jts_pos()
                for i in xrange(cvs_array.length()):
                    # ----------------------------------------
                    # get the weighted matrix
                    euler_per_joint = []
                    for j in xrange(self._dpJoints.length()):
                        inclusive_mat = self._dpJoints[j].inclusiveMatrix()
                        euler_per_joint.append(om.MTransformationMatrix(inclusive_mat).rotation().asEulerRotation())

                    # - get the offset matrix (cv * base_cv. The baseCV mat has
                    #   a position 0,0,0, so with only 1 matrix mult, we get the 
                    #   offset in the correct position in space instead of 
                    #   having it in the origin
                    weighted_matrix      = self.get_weighted_matrix(euler_per_joint, self._weights[i], cvs_array[i])
                    weighted_base_matrix = self._base_mats_per_cv[i]
                    offset_mat = weighted_matrix * weighted_base_matrix.inverse()
                    weighted_matrices.append(weighted_matrix)

                    # adds the delta
                    # It is super important to work with MPoints and not 
                    # MVectors for the deltas, as an MPoint*MMatrix gives 
                    # different result from MVector*MMatrix
                    delta = self._pOffsets[itGeo.index()]
                    final_pos = delta * offset_mat

                    # stores the final cv position
                    offset_cvs.append([final_pos.x, final_pos.y, final_pos.z])

                # correct
                do = 99
                if itGeo.index() == do:
                    tau = self.get_tau(P, O, Q, om.MPoint(itGeo.position()))
                    default_tau = self._default_taus[itGeo.index()]
                    tau = tau - default_tau
                    # print 'tau -->', tau
                    # print 'weight -->', self._dist_CV_weights
                    # tau = self._remap(tau, default_tau-.5, default_tau+.5, -1., 1.)
                    # self.draw_point(itGeo.position(), [tau, 0, 0])

                    # get the CVs by distance
                    # weighted_cvs = self._dist_CV_weights[itGeo.index()]
                    weighted_cvs = self._dist_CV_weights
                    offset_cvs = self.offset_CVs_by_tau(itGeo.position(), 
                                                        offset_cvs, 
                                                        weighted_matrices, 
                                                        tau, 
                                                        weighted_cvs,
                                                        self._directions_mat[itGeo.index()])
                    [self.draw_point(cv) for cv in offset_cvs]

                # now we have the new CP positions, compute the curve
                crv = nurbsCurve.NurbsCurve(points=offset_cvs, knots=knots, degree=degree, weights=weights)
                new_pos = crv.pt_at_param(self._params[itGeo.index()])



                out_positions.append(om.MPoint(new_pos[0], new_pos[1], new_pos[2]))

                itGeo.next()

            itGeo.setAllPositions(out_positions)
    
    def get_skin_cluster(self):
        '''
        Also returns the dag path to the inCurve, that is needed for 
        skinCluster.getWeights()
        '''
        # 1 - get the skinCluster attached to the in curve
        # - first, get the plug of the curve
        fnDep = om.MFnDependencyNode(self.thisMObject())
        pInCrv = fnDep.findPlug(curveDeformer.aInCrv)
        itDg = om.MItDependencyGraph(pInCrv, om.MItDependencyGraph.kDownstream, om.MItDependencyGraph.kPlugLevel)
        
        # - then, get the curve as a MDagPath. The reason why we do all this
        #   crap is because the dataHandle.asNurbsCurve() returns a 
        #   kNurbsCurveData, and MDagPath or everything else requires a kNurbsCurve.
        dpInCrv = om.MDagPath()
        plugs = om.MPlugArray()
        pInCrv.connectedTo(plugs, True, False)
        plug = plugs[0]
        oInCrv_asNurbsCurve = plug.node()
        fnDagInCrv = om.MFnDagNode(oInCrv_asNurbsCurve)
        fnDagInCrv.getPath(dpInCrv)
        
        # - now we have this dag path, let's grab the skinCluster
        scs = []
        while not itDg.isDone():
            oCurrent = itDg.currentItem()
            if oCurrent.hasFn(om.MFn.kSkinClusterFilter):
                scs.append(oCurrent)
            itDg.next()
        if not scs:
            raise TypeError, 'No skin cluster found. Returns'
        fnSc = omAnim.MFnSkinCluster(scs[0])

        return fnSc, dpInCrv

    def get_skin_weights(self, fnSc, dpInCrv):
        ''' 
        Returns an array of weights for each CV
        :param fnSc: function set of the skinCluster
        :type  fnSc: MFnSkinCluster
        :param dpInCrv: dag path to the curve attached to the skinCluster
        :return : list of MDoubleArray
        '''
        weights = []
        itCv = om.MItCurveCV(dpInCrv)
        outArray = om.MDoubleArray()
        nbInflUtil = om.MScriptUtil()
        nbInflUtil.createFromInt(0)
        while not itCv.isDone():
            outArray.clear()
            fnSc.getWeights(dpInCrv, itCv.currentItem(), outArray, nbInflUtil.asUintPtr())
            weights.append(list(outArray))
            itCv.next()
        return weights

    def inverse_distance_weighting(self, pt, poses, p=2):
        '''
        Returns a vector of normalized weights, based on the 
        inverse distance to the reference point. If one of the 
        poses is on the reference point (i.e. distance=0), 
        we can't do the invert 1/0, so we edit manually the 
        vector to have something like [0,...,0,1,0,...]
        Ex : pt = np.zeros([3])
             poses = np.array([[-2,0,0],
                               [-1,0,0],
                               [0,0,0],
                               [2,0,0]])
        wts = inverse_distance_weighting(o, poses)

        :param    pt: reference point, that is responsible for the weighing
        :type     pt: np.array of n elements
        :param poses: matrix of shape MxN of each pose we want to assign a weight to
        :type  poses: np.array of MxN elements
        :return: np.array of n elements, representing the weights
        '''
        num_poses = poses.length()
        weights_vec = np.zeros([num_poses])
        for i in xrange(num_poses):
            cv = np.array([poses[i].x, poses[i].y, poses[i].z])
            dist = np.linalg.norm(pt - cv)
            inv_dist = 1./ np.power(dist, p) if dist != 0 else -1
            weights_vec[i] = inv_dist
        if -1 in weights_vec:
            for i in xrange(len(weights_vec)):
                if weights_vec[i] == -1:
                    weights_vec[i] = 1 
                else:
                    weights_vec[i] = 0
        weights_vec = weights_vec / np.sum(weights_vec)
        return weights_vec

    def get_mat_per_cv(self, dpJoints, cvs_array):
        ''' Computes an average of the weight for each CV, to build 
        a single matrix that is the orientation of the current CV.
        This matrix is the weighted sum of all the joints that influence
        this CV. Right now, it is using euler.
        TODO - use quaternions to blend the different matrices
               weighted quats implementation : 
               https://stackoverflow.com/questions/12374087/average-of-multiple-quaternions
        :param dpJoints: dag path array for all the joints influencing the curve
        :type  dpJoints: MDagPathArray
        '''
        euler_per_joint = []
        for j in xrange(dpJoints.length()):
            inclusive_mat = dpJoints[j].inclusiveMatrix()
            euler_per_joint.append(om.MTransformationMatrix(inclusive_mat).rotation().asEulerRotation())
        
        base_mats_per_cv = om.MMatrixArray()
        for i in xrange(cvs_array.length()):
            # get the weighted matrix, using euler
            transf_mat = self.get_weighted_matrix(euler_per_joint, self._weights[i])#, cvs_array[i])
            base_mats_per_cv.append(transf_mat)
        return base_mats_per_cv

    def get_offsets_and_params(self, itGeo, fnBaseCrv):
        ''' 
        Computes the offset between each vertex and the closest point on 
        the curve. And since it's the same command, compute the param of 
        the closest point on curve at the same time, to feed the nurbsCurve 
        algorithm with it
        '''
        util = om.MScriptUtil()
        util.createFromDouble(0.)
        uPtr = util.asDoublePtr()
        offsets = om.MPointArray()
        params  = []
        all_base_mats = []
        while not itGeo.isDone():
            # - offset & parameter
            pos = itGeo.position()
            pt_on_base_crv = fnBaseCrv.closestPoint(pos, uPtr)
            param = om.MScriptUtil.getDouble(uPtr)
            vOffset = pos - pt_on_base_crv
            offsets.append(om.MPoint(vOffset))
            params.append(param)

            # all_base_mats.append(self._base_mats_per_cv)
            itGeo.next()
        itGeo.reset()

        return offsets, params

    def get_weighted_matrix(self, eulers, weights, pos=None):
        ''' 
        Takes an array of eulers, an array of weights of the same size,
        and outputs one matrix based on the input weights (and the pos)
        The rotation is done by interpolating each joint, but the translate is 
        given (usually the position of the CP)
        '''
        outX, outY, outZ = 0., 0., 0.
        for i in xrange(len(weights)):
            curr_euler  = eulers[i]
            curr_weight = weights[i]
            outX += curr_euler.x * curr_weight
            outY += curr_euler.y * curr_weight
            outZ += curr_euler.z * curr_weight
        outEuler = om.MEulerRotation(outX, outY, outZ)
        outMatrix = outEuler.asMatrix()
        # add the translates if they are provided
        if pos:
            transf_mat = om.MTransformationMatrix(outMatrix)
            transf_mat.setTranslation(om.MVector(pos), om.MSpace.kWorld)
            return transf_mat.asMatrix()
        else:
            return outMatrix

    def get_3_closest_jts_per_vertex(self, itGeo, joints_pos):
        ''' In order to compute Tau, we need to compute the angle 
        at the elbow, between shoulder and wrist. This is in an ideal setup
        with only 3 bones. But if we have more than 3 bones, to do the same 
        computation, we need to make sure we work on the correct set of 3 bones.
        Therefore, we need to check the 3 bones we'll used, regarding the 
        hierarchy : 
        Situation 1 - get the closest bone
                      get its child 
                      get its parent
        Situation 2 - get the closest bone
                      get its child -> NO CHILD
                      get its parent
                      get the parent's parent.
        Situation 3 - get the closest bone
                      get its child
                      get its parent -> NO PARENT
                      get the child's child.

        :param      itGeo: iterator for the geometry
        :type       itGeo: MItGeometry
        :param joints_pos: XYZ coordinates for each joint
        :type  joints_pos: list of np.array
        :param joints_hierarchy: list of joints, sorted by parent/child
        :type  joints_hierarchy: list of np.array  
        :return          : a nested list containing for each vertex a list of 3 indices
        :return type     : list
        '''
        # iter through each vertex to keep the 3 closest joints
        all_closest_jts_idx = []
        while not itGeo.isDone():
            pos = itGeo.position()
            pos = np.array([pos.x, pos.y, pos.z])
            dists = []
            for i in xrange(len(joints_pos)):
                dists.append(np.linalg.norm(joints_pos[i] - pos))

            sorted_dists = sorted(dists)
            closest_jt_idx = dists.index(sorted_dists[0])
            # - get the child
            child_jt_idx = closest_jt_idx+1
            # - get the parent
            parent_jt_idx = closest_jt_idx-1
            
            # Situation 1
            closest_3_jts_idx = [parent_jt_idx, closest_jt_idx, child_jt_idx]
            # Situation 2 - no child available
            if closest_jt_idx >= len(joints_pos)-1:
                closest_3_jts_idx = [parent_jt_idx-1, parent_jt_idx, closest_jt_idx]
            # Situation 3 - no parent available
            if closest_jt_idx == 0:
                closest_3_jts_idx = [closest_jt_idx, child_jt_idx, child_jt_idx+1]

            # sorted_dists = sorted_dists[:3]
            # closest_3_jts_idx =  [dists.index(sorted_dists[0]),
            #                       dists.index(sorted_dists[1]),
            #                       dists.index(sorted_dists[2])]
            all_closest_jts_idx.append(closest_3_jts_idx)
            itGeo.next()

        itGeo.reset()

        return all_closest_jts_idx

    def get_default_taus(self, itGeo, P, O, Q):
        '''
        compute the default Tau value for each vertex, in order to know how to 
        remap it later, in the deform (if we're in the middle of the elbow, 
        tau may be .5, but on the sides, it may be 1.2. So we need to call a 
        remap with different values)
        '''
        out_taus = np.zeros([itGeo.count()])
        while not itGeo.isDone():
            out_taus[itGeo.index()] = self.get_tau(P, O, Q, itGeo.position())
            itGeo.next()
        itGeo.reset()
        return out_taus

    def get_tau(self, p_idx, o_idx, q_idx, vR):
        ''' 
        To know how much the CVs need to be offset to sharpen / smooth 
        the curve, we compute a parameter at bind pause (we call it Tau), that 
        will vary based on the bend angle between each joint. In this setup, 
        shoulder, elbow and wrist positions are respectively P, O and Q. The 
        vertex we currently compute is known as R
        :param p_pos: position of the bone P, usually the shoulder
        :type  p_pos: np.array
        :param o_pos: position of the bone P, usually the elbow, which rotates
        :type  o_pos: np.array
        :param q_pos: position of the bone P, usually the wrist
        :type  q_pos: np.array
        :param r_pos: position of the vertex we use to get tau
        :type  r_pos: np.array
        :return     : float
        '''
        p_pos = np.array(self.jts_pos[p_idx])
        o_pos = np.array(self.jts_pos[o_idx])
        q_pos = np.array(self.jts_pos[q_idx])
        r_pos = np.array([vR.x, vR.y, vR.z])

        # Eq. 1-3
        p = p_pos - o_pos
        p_norm = p / np.linalg.norm(p)
        q = q_pos - o_pos
        q_norm = q / np.linalg.norm(q)
        r = r_pos - o_pos
        r_norm = r / np.linalg.norm(r)

        # Eq. 4-5
        theta = np.arccos(r_norm.dot(q_norm))  # angle ROQ en radians
        alpha_min = np.arccos(p_norm.dot(q_norm)) # angle POQ en radians

        # Eq. 6 - make sure we always have the smaller angle
        cross_pq = np.cross(p_norm, q_norm)
        cross_rq = np.cross(r_norm, q_norm)
        
        # cross_pq_norm = cross_pq / np.linalg.norm(cross_pq) # pas besoin de normaliser je crois
        # cross_rq_norm = cross_rq / np.linalg.norm(cross_rq)
        # if cross_pq_norm.dot(cross_rq_norm) >= 0:
        if cross_pq.dot(cross_rq) >= 0:
            alpha = alpha_min
        else:
            alpha = 2*np.pi - alpha_min

        # alpha est flat quand alpha * (np.pi/alpha) == np.pi
        theta_flat = theta * (np.pi/alpha)
        # Eq. 9
        epsilon = np.linalg.norm(r) * np.cos(theta_flat)  # epsilon = distance depuis O jusqu'a la perpidenculaire a OQ en R

        # Eq. 10
        a = np.linalg.norm(p)
        b = np.linalg.norm(q)
        numerator = a + a*min(0, epsilon) + b*max(0, epsilon)
        # numerator = a + np.linalg.norm(p*min(0, epsilon)) + np.linalg.norm(q*max(0, epsilon))
        denominator = a + b
        tau = numerator / denominator
        
        return tau

    def set_offset_direction(self, itGeo, offsets, base_cvs, base_mat_bones):
        '''
        In order to know in which direction we'll push the CVs (using Tau) in 
        the deform, we set a matrix of values (+1 or -1) that we'll 
        use in offset_CVs_by_tau() later. In a nutshell, we get the vector 
        offset_CV->vertex, and we compare it against the main orient axis of the
        bone (usually -and hardcoded here- +X), and the neg main orient axis 
        (-X). Then, with cosine similarity, we define if offset_CV-> vertex is 
        closer from +X or -X.
        Returns a m x n matrix with m = number of vertices and n = number of CVs
        each value is either 1 or -1, depending if we wanna push or pull the CV
        '''
        num_cvs = base_cvs.length() # MPointArray()
        out_mat = np.ones([itGeo.count(), num_cvs])

        bone_aim_axis = np.array([1,0,0,0])  # hardcoded for now : bones are oriented in +X
        while not itGeo.isDone():
            i = itGeo.index()
            vtx_pos = itGeo.position()
            for j in xrange(num_cvs):
                base_cv  = base_cvs[j]
                offset_cv= base_cv + om.MVector(offsets[i])
                base_mat = self.MMatrix_to_np_mat(base_mat_bones[j])

                # get the normalized base vector cv->vertex
                cv_to_pos = vtx_pos - offset_cv
                np_cv_to_pos = np.array([cv_to_pos.x, cv_to_pos.y, cv_to_pos.z])
                if np.linalg.norm(np_cv_to_pos) != 0:
                    np_cv_to_pos = np_cv_to_pos / np.linalg.norm(np_cv_to_pos)
                    
                    # get the orientation (i.e. should we push the CV in x or pull it?)
                    base_bone_orient_pos = self.filter_matrix_axis(base_mat, bone_aim_axis)
                    base_bone_orient_pos = base_bone_orient_pos / np.linalg.norm(base_bone_orient_pos)
                    base_bone_orient_neg = -base_bone_orient_pos
                    pos_x = np_cv_to_pos.dot(base_bone_orient_pos)
                    neg_x = np_cv_to_pos.dot(base_bone_orient_neg)
                    if pos_x > neg_x:
                        out_mat[i, j] = +1
                    else:
                        out_mat[i, j] = -1
                else:
                    out_mat[i, j] = 0

            itGeo.next()

        itGeo.reset()
        return out_mat

    def offset_CVs_by_tau(self, pos, offset_cvs, mat_bones, tau, cv_weights, cv_direction):
        '''
        We computed, in the init, whether we should pull or push the CV.
        To know of how much we move the CV, we multiply the current bone aim 
        vector by the pre-computed direction (i.e. +1 or -1) by a remapped value of 'tau'
        :param offset_cvs: offset cvs, after we applied the delta vector to them
        '''
        vtx_pos = np.array([pos.x, pos.y, pos.z])
        bone_aim_axis = np.array([1,0,0,0])  # hardcoded for now : bones are oriented in +X

        num_cvs = len(offset_cvs)
        out_cvs = np.zeros([num_cvs, 3])

        for i in xrange(num_cvs):
            offset_cv       = offset_cvs[i]
            mat_bone        = self.MMatrix_to_np_mat(mat_bones[i])
            bone_aim_vector = self.filter_matrix_axis(mat_bone, bone_aim_axis)
            bone_aim_vector = bone_aim_vector / np.linalg.norm(bone_aim_vector)

            # offset_vector = om.MPoint(*(bone_aim_vector * cv_weights[i] * cv_direction[i] * tau))
            offset_vector = om.MPoint(*(bone_aim_vector * cv_weights[i] * tau * -1))
            # offset_vector = om.MPoint(*(bone_aim_vector * cv_direction[i] * tau))
            new_cv = om.MPoint(*offset_cv) + om.MVector(offset_vector)
            # if i == 4:
            #     self.draw_vector(vec_cv_to_pos, pos=cv)

            out_cvs[i] = [new_cv.x, new_cv.y, new_cv.z]

        return out_cvs

    # ---------------------- No longer used ------------------------
    def weight_with_rbf(self, n, point, sigma=1):
        num_points = len(n)
        f = np.zeros([num_points])
        for i in xrange(num_points):
            pose = n[i]
            dist = np.linalg.norm(pose-point)
            gaussian = np.exp(-np.power(dist*sigma, 2))
            f[i] = gaussian

        ii=np.zeros([num_points, num_points])
        for i in xrange(num_points):
            jj=[]
            posei=n[i]
            for j in xrange(num_points):
                posej=n[j]
                dist = np.linalg.norm(posei-posej)
                gaussian = np.exp(-np.power(dist, 2))
                ii[j, i] = gaussian
        poseWeights = np.linalg.solve(ii, f)
        return poseWeights

    def assign_weight_per_offset_cv(self, itGeo, base_cvs, deltas):
        '''
        Assigns a weight for each offset CV, based on the invert distance to 
        the deformed vertex
        TODO : when the CV is on the vertex, we encounter a division by 0. 
               Need to fix that
        :param itGeo: geo iterator
        :type  itGeo: MItGeometry
        :param base_cvs: position of the CVs of the base curve 
                         (i.e. list of n 3D-coord, with n=number of CVs)
        :type  base_cvs: MPointArray
        :param deltas: delta b/w the deformed vertex and the closest pt on crv
        :type  deltas: MPointArray
        '''
        num_cvs = base_cvs.length()
        weights_mat = np.zeros([itGeo.count(), num_cvs])
        while not itGeo.isDone():
            # first, offset the curve (and convert everything to numpy...)
            offset_cvs = np.zeros([num_cvs])
            delta     = np.array([deltas[itGeo.index()].x, 
                                  deltas[itGeo.index()].y,
                                  deltas[itGeo.index()].z])
            vtx_pos   = np.array([itGeo.position().x, 
                                  itGeo.position().y, 
                                  itGeo.position().z])

            weight_sum = 0
            for i in xrange(num_cvs):
                cv = np.array([base_cvs[i].x, base_cvs[i].y, base_cvs[i].z])
                # offset_cv = cv + delta
                # then, weight it
                inv_dist = 1./ np.power(np.linalg.norm(vtx_pos-cv), 4)
                offset_cvs[i] = inv_dist
                weight_sum += inv_dist

            norm_offset_cvs = offset_cvs / weight_sum
            # and finally, store the weights of this curve in a matrix of 
            # m x n where m=number of vertices and n=number of CVs of the base crv
            weights_mat[itGeo.index()] = norm_offset_cvs
            itGeo.next()
        
        itGeo.reset()
        return weights_mat

    def assign_weight_per_cv(self, O, base_cvs):
        num_cvs = base_cvs.length()
        weights_vec = np.zeros([num_cvs])
        weight_sum = 0.0
        for i in xrange(num_cvs):
            cv = np.array([base_cvs[i].x, base_cvs[i].y, base_cvs[i].z])
            # then, weight it
            dist = np.linalg.norm(O - cv)
            inv_dist = 1./ np.power(dist, 4)
            weights_vec[i] = inv_dist
            weight_sum += inv_dist
        weights_vec = weights_vec / weight_sum
        return weights_vec

    # ---------------------- debug/convenient functions ------------------------
    def filter_matrix_axis(self, matrix, vector):
        ''' 
        Returns the correct axis of the matrix, based on the input vector.
        Ex : on a matrix [[1,2,3,0], [4,5,6,0], [7,8,9,0], [0,0,0,1]], with the
        vector [0,1,0,0], will return only the Y axis, [4,5,6]
        :param matrix: input matrix we want to filter
        :type  matrix: np.array (in 2D)
        :param vector: vector array used to filter the matrix
        :type  vector: np.array
        '''
        for axis in xrange(len(matrix)):
            output = matrix[axis] * vector[axis]
            if any([output[i] != 0 for i in xrange(4)]):
                return output[:3]

    def MMatrix_to_np_mat(self, matrix):
        return np.array([[matrix(j, i) for i in xrange(4)] for j in xrange(4)])

    def _remap(self, value, oldMin, oldMax, newMin, newMax):
        return (((value - oldMin) * (newMax - newMin)) / (oldMax - oldMin)) + newMin

    def printVector(self, v):
        if isinstance(v, om.MPoint) or isinstance(v, om.MVector):
            return [round(x, 2) for x in (v.x, v.y, v.z)]
        else:
            return [round(x, 2) for x in v]

    def printMat(self, mat):
        for i in xrange(4):
            print [round(mat(i, j), 2) for j in xrange(4)]

    def draw_point(self, point, color=[255, 255, 0]):
        view = OpenMayaUI.M3dView.active3dView()
        view.beginGL()
        glFT.glPointSize(5.8)
        glFT.glBegin(OpenMayaRender.MGL_POINTS)
        glFT.glColor3f(*color)
        if isinstance(point, om.MPoint) or isinstance(point, om.MVector):
            glFT.glVertex3f(point.x, point.y, point.z)
        else:
            glFT.glVertex3f(point[0], point[1], point[2])
        glFT.glEnd()

        offset = .01
        if isinstance(point, om.MPoint) or isinstance(point, om.MVector):
            glFT.glBegin(OpenMayaRender.MGL_LINES)
            glFT.glVertex3f(point.x-offset, point.y, point.z)
            glFT.glVertex3f(point.x+offset, point.y, point.z)
            glFT.glEnd()
            glFT.glBegin(OpenMayaRender.MGL_LINES)
            glFT.glVertex3f(point.x, point.y-offset, point.z)
            glFT.glVertex3f(point.x, point.y+offset, point.z)
            glFT.glEnd()
            glFT.glBegin(OpenMayaRender.MGL_LINES)
            glFT.glVertex3f(point.x-offset, point.y, point.z-offset)
            glFT.glVertex3f(point.x+offset, point.y, point.z+offset)
            glFT.glEnd()
        else:
            glFT.glBegin(OpenMayaRender.MGL_LINES)
            glFT.glVertex3f(point[0]-offset, point[1], point[2])
            glFT.glVertex3f(point[0]+offset, point[1], point[2])
            glFT.glEnd()
            glFT.glBegin(OpenMayaRender.MGL_LINES)
            glFT.glVertex3f(point[0], point[1]-offset, point[2])
            glFT.glVertex3f(point[0], point[1]+offset, point[2])
            glFT.glEnd()
            glFT.glBegin(OpenMayaRender.MGL_LINES)
            glFT.glVertex3f(point[0]-offset, point[1], point[2]-offset)
            glFT.glVertex3f(point[0]+offset, point[1], point[2]+offset)
            glFT.glEnd()
        view.endGL()

    def draw_vector(self, v, pos=[0,0,0], color=[255,255,255]):
        view = OpenMayaUI.M3dView.active3dView()
        view.beginGL()
        glFT.glColor3f(*color)
        glFT.glBegin(OpenMayaRender.MGL_LINES)
        if isinstance(pos, om.MPoint) or isinstance(pos, om.MVector):
            start = [pos.x, pos.y, pos.z]
        else:
            start = pos

        if isinstance(v, om.MPoint) or isinstance(v, om.MVector):
            end = [start[0] + v.x, start[0] + v.y, start[0] + v.z]
        else:
            end = [start[0] + v[0], start[1] + v[1], start[2] + v[2]]

        glFT.glVertex3f(*start)
        glFT.glVertex3f(*end)

        glFT.glEnd()
        view.endGL()

    def draw_curve(self, nurbsCurve, color=[randint(0,100)/100. for _ in xrange(3)]):
        raise DeprecationWarning ('No longer useful, since we use VP2.0')
        # view = OpenMayaUI.M3dView.active3dView()
        # view.beginGL()
        # pts = nurbsCurve.compute_crv()
        # for i in xrange(len(pts)):
        #     if i == 0:
        #         continue
        #     glFT.glColor3f(*color)
        #     glFT.glLineWidth(1)
        #     glFT.glBegin(OpenMayaRender.MGL_LINES)
        #     glFT.glVertex3f(pts[i-1][0], pts[i-1][1], pts[i-1][2])
        #     glFT.glVertex3f(pts[i][0], pts[i][1], pts[i][2])
        #     glFT.glEnd()

        # view.endGL()

def nodeCreator():
    return omMpx.asMPxPtr(curveDeformer())

def nodeInitializer():
    tAttr = om.MFnTypedAttribute()
    nAttr = om.MFnNumericAttribute()
    mAttr = om.MFnMatrixAttribute()
    cAttr = om.MFnCompoundAttribute()

    # init
    curveDeformer.aInit = nAttr.create('initialize', 'init', om.MFnNumericData.kBoolean, True)
    curveDeformer.addAttribute(curveDeformer.aInit)
    nAttr.setChannelBox(True)

    # inCurve
    curveDeformer.aInCrv = tAttr.create('inputCurve', 'inCrv', om.MFnData.kNurbsCurve)
    curveDeformer.addAttribute(curveDeformer.aInCrv)

    # baseCurve 
    curveDeformer.aBaseCrv = tAttr.create('baseCurve', 'baseCrv', om.MFnData.kNurbsCurve)
    curveDeformer.addAttribute(curveDeformer.aBaseCrv)
    
    # curve controllers - now used only for tweaking the weight of each CV
    curveDeformer.aWeight = nAttr.create('weight', 'wgt', om.MFnNumericData.kFloat, 1.)
    nAttr.setKeyable(True)
    nAttr.setMin(.001)
    curveDeformer.aCps = cAttr.create('controlPoints', 'cps')
    cAttr.addChild(curveDeformer.aWeight)
    cAttr.setArray(True)
    curveDeformer.addAttribute(curveDeformer.aCps)

    # connected joints (used to compute Tau)
    curveDeformer.aMatrixJoint = mAttr.create('matrixJoint', 'matJt')
    curveDeformer.aMatrixJoints = cAttr.create('matrixJoints', 'matJts')
    cAttr.addChild(curveDeformer.aMatrixJoint)
    cAttr.setArray(True)
    curveDeformer.addAttribute(curveDeformer.aMatrixJoints)

    # attribute effects
    curveDeformer.attributeAffects(curveDeformer.aInit, curveDeformer.outputGeom)
    curveDeformer.attributeAffects(curveDeformer.aInCrv, curveDeformer.outputGeom)
    curveDeformer.attributeAffects(curveDeformer.aBaseCrv, curveDeformer.outputGeom)
    curveDeformer.attributeAffects(curveDeformer.aCps, curveDeformer.outputGeom)
    curveDeformer.attributeAffects(curveDeformer.aMatrixJoints, curveDeformer.outputGeom)

    # make deformer paintable
    om.MGlobal.executeCommand("makePaintable -attrType multiFloat -sm deformer curveDeformer ws;")

def initializePlugin(mObj):
    plugin = omMpx.MFnPlugin(mObj, 'fruity', '1.0', 'any')
    try:
        plugin.registerNode(pluginName, pluginId, nodeCreator, nodeInitializer, omMpx.MPxNode.kDeformerNode)
    except:
        sys.stderr.write('Load plugin failed: %s' % pluginName)

def uninitializePlugin(mObj):
    plugin = omMpx.MFnPlugin(mObj)
    try:
        plugin.deregisterNode(pluginId)
    except:
        sys.stderr.write('Unload plugin failed: %s' % pluginName)

