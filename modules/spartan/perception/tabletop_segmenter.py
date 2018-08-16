#!/usr/bin/env python
from __future__ import print_function

import argparse
import cv2
import matplotlib.pyplot as plt
import numpy as np
import os
import sys
import matplotlib.pyplot as plt

import geometry_msgs.msg
import ros_numpy
import rospy
import sensor_msgs.msg

import spartan.utils.utils as spartanUtils
import spartan.utils.ros_utils as rosUtils

from director import filterUtils
from director import ioUtils
from director import transformUtils
from director.shallowCopy import shallowCopy
from director import vtkNumpy
from director import vtkAll as vtk

import meshcat
import meshcat.geometry as meshcat_g
import meshcat.transformations as meshcat_tf

import octomap


def do_pointcloud_preprocessing(pc2):
    pc_np = ros_numpy.numpify(pc2)

    # First, back out the depth image and get a mask
    # for where we have no returns
    nan_mask = np.logical_not(np.isfinite(pc_np['z']))
    nan_mask = np.stack([nan_mask, nan_mask, nan_mask], axis=-1)

    xyz_im = np.stack([pc_np['x'], pc_np['y'], pc_np['z']], axis=-1)
    xyz_im[nan_mask] = 30.
    xyz_im_avg = xyz_im.copy()
    for k in range(10):
        for l in range(3):
            xyz_im_avg[:, :, l] = cv2.blur(xyz_im_avg[:, :, l], (5, 5))
        xyz_im_avg = np.where(nan_mask, xyz_im_avg, xyz_im)
    ksize = 11
    normal_xx = cv2.Sobel(xyz_im_avg[:, :, 0], cv2.CV_32F, 1, 0, ksize=ksize)
    normal_xy = cv2.Sobel(xyz_im_avg[:, :, 0], cv2.CV_32F, 0, 1, ksize=ksize)
    normal_yx = cv2.Sobel(xyz_im_avg[:, :, 1], cv2.CV_32F, 1, 0, ksize=ksize)
    normal_yy = cv2.Sobel(xyz_im_avg[:, :, 1], cv2.CV_32F, 0, 1, ksize=ksize)
    normal_zx = cv2.Sobel(xyz_im_avg[:, :, 2], cv2.CV_32F, 1, 0, ksize=ksize)
    normal_zy = cv2.Sobel(xyz_im_avg[:, :, 2], cv2.CV_32F, 0, 1, ksize=ksize)

    x_normals = np.stack([normal_xx, normal_yx, normal_zx], axis=-1)
    y_normals = np.stack([normal_xy, normal_yy, normal_zy], axis=-1)
    normals = -np.cross(x_normals, y_normals)
    ln = np.linalg.norm(normals, axis=-1)
    normals = normals / np.stack([ln, ln, ln], axis=-1)

    # Reject stuff with normals too far from the camera depth axis
    _, reject_im = cv2.threshold(np.dot(normals, [0., 0., -1.]),
                                 0.2, 1., cv2.THRESH_BINARY)
    reject_im = reject_im.astype(bool)
    reject_im = np.stack([reject_im, reject_im, reject_im], axis=-1)

    points = np.stack([pc_np['x'], pc_np['y'], pc_np['z']], axis=-1)
    points = np.where(reject_im, points, np.nan)
    normals = np.where(reject_im, normals, np.nan)

    plt.subplot(4, 3, 1)
    plt.imshow(xyz_im_avg[:, :, 0])
    plt.subplot(4, 3, 2)
    plt.imshow(xyz_im_avg[:, :, 1])
    plt.subplot(4, 3, 3)
    plt.imshow(xyz_im_avg[:, :, 2])
    plt.subplot(4, 3, 3+1)
    plt.imshow(normals[:, :, 0])
    plt.subplot(4, 3, 3+2)
    plt.imshow(normals[:, :, 1])
    plt.subplot(4, 3, 3+3)
    plt.imshow(normals[:, :, 2])
    plt.subplot(8, 3, 13)
    plt.imshow(normal_xx)
    plt.subplot(8, 3, 14)
    plt.imshow(normal_xy)
    plt.subplot(8, 3, 15)
    plt.imshow(normal_yx)
    plt.subplot(8, 3, 16)
    plt.imshow(normal_yy)
    plt.subplot(8, 3, 17)
    plt.imshow(normal_zx)
    plt.subplot(8, 3, 18)
    plt.imshow(normal_zy)
    plt.subplot(4, 1, 4)
    plt.imshow(reject_im)
    plt.pause(1E-6)

    return points, normals


def flatten_3d_image_consistently(im):
    # reshape is a little sketchy because of memory ordering questions
    return np.stack((im[:, :, 0].flatten(),
                     im[:, :, 1].flatten(),
                     im[:, :, 2].flatten()), axis=-1)


def convert_pc_np_to_vtk(points, normals):
    n_points = points.shape[0]*points.shape[1]
    points_flat = flatten_3d_image_consistently(points)
    normals_flat = flatten_3d_image_consistently(normals)
    good_entries = np.logical_not(np.isnan(points_flat, ).any(axis=1))
    points_flat = points_flat[good_entries, :]
    normals_flat = normals_flat[good_entries, :]
    pc_vtk = vtkNumpy.numpyToPolyData(
        points_flat, pointData=None, createVertexCells=True)
    vtkNumpy.addNumpyToVtk(pc_vtk, normals_flat, "Normals")
    return pc_vtk


def applyEuclideanClustering(dataObj, clusterTolerance=0.05,
                             minClusterSize=100, maxClusterSize=1e6):
    # COPIED FROM DIRECTOR/SEGMENTATIONROUTINES
    # (which I can't import because importing PythonQt is broken)
    f = vtk.vtkPCLEuclideanClusterExtraction()
    f.SetInputData(dataObj)
    f.SetClusterTolerance(clusterTolerance)
    f.SetMinClusterSize(int(minClusterSize))
    f.SetMaxClusterSize(int(maxClusterSize))
    f.Update()
    return shallowCopy(f.GetOutput())


def extractClusters(polyData, clusterInXY=False, **kwargs):
    ''' Segment a single point cloud into smaller clusters
        using Euclidean Clustering
     '''

    if not polyData.GetNumberOfPoints():
        return []

    if clusterInXY is True:
        ''' If Points are seperated in X&Y, then cluster outside this '''
        polyDataXY = vtk.vtkPolyData()
        polyDataXY.DeepCopy(polyData)
        points = vtkNumpy.getNumpyFromVtk(polyDataXY, 'Points')
        points[:, 2] = 0.0
        polyDataXY = applyEuclideanClustering(polyDataXY, **kwargs)
        clusterLabels = vtkNumpy.getNumpyFromVtk(polyDataXY, 'cluster_labels')
        vtkNumpy.addNumpyToVtk(polyData, clusterLabels, 'cluster_labels')

    else:
        polyData = applyEuclideanClustering(polyData, **kwargs)
        clusterLabels = vtkNumpy.getNumpyFromVtk(polyData, 'cluster_labels')

    clusters = []
    for i in xrange(1, clusterLabels.max() + 1):
        cluster = filterUtils.thresholdPoints(polyData, 'cluster_labels',
                                              [i, i])
        clusters.append(cluster)
    return clusters


def applyVoxelGrid(polyData, leafSize=0.01):

    v = vtk.vtkPCLVoxelGrid()
    v.SetLeafSize(leafSize, leafSize, leafSize)
    v.SetInputData(polyData)
    v.Update()
    return shallowCopy(v.GetOutput())


def getMajorPlanes(polyData, useVoxelGrid=False,
                   voxelGridSize=0.01,
                   distanceToPlaneThreshold=0.02):

    if useVoxelGrid:
        polyData = applyVoxelGrid(polyData, leafSize=voxelGridSize)

    polyDataList = []

    minClusterSize = 100

    planeInfo = []
    while len(polyDataList) < 5:
        f = vtk.vtkPCLSACSegmentationPlane()
        f.SetInputData(polyData)
        f.SetDistanceThreshold(distanceToPlaneThreshold)
        f.Update()
        polyData = shallowCopy(f.GetOutput())

        outliers = filterUtils.thresholdPoints(polyData, 'ransac_labels', [0, 0])
        inliers = filterUtils.thresholdPoints(polyData, 'ransac_labels', [1, 1])
        largestCluster = extractLargestCluster(inliers)

        if largestCluster.GetNumberOfPoints() > minClusterSize:
            polyDataList.append(largestCluster)
            polyData = outliers
            planeInfo.append((np.array(f.GetPlaneOrigin()),
                              np.array(f.GetPlaneNormal())))
        else:
            break

    return polyDataList, planeInfo


def extractLargestCluster(polyData, **kwargs):
    '''
    Calls applyEuclideanClustering and then extracts the first (largest) cluster.
    The given keyword arguments are passed into the applyEuclideanClustering function.
    '''
    polyData = applyEuclideanClustering(polyData, **kwargs)
    return filterUtils.thresholdPoints(polyData, 'cluster_labels', [1, 1])


def draw_polydata_in_meshcat(vis, polyData, name, color=None, size=0.001,
                             with_normals=False):
    points = vtkNumpy.getNumpyFromVtk(polyData).T
    if color is not None:
        colors = np.tile(color[0:3], [points.shape[1], 1]).T
    else:
        colors = np.zeros(points.shape) + 1.
    vis["perception"]["tabletopsegmenter"][name].set_object(
        meshcat_g.PointCloud(position=points, color=colors, size=size))
    if with_normals:
        normals = vtkNumpy.getNumpyFromVtk(polyData, "Normals").T
        colors = np.tile([1., 0., 0.], [points.shape[1], 1]).T
        segments = np.zeros((3, normals.shape[1]*2))
        for k in range(normals.shape[1]):
            segments[:, 2*k] = points[:, k]
            segments[:, 2*k+1] = points[:, k] + normals[:, k]*0.05
        vis["perception"]["tabletopsegmenter"][name]["normals"].set_object(
            meshcat_g.LineSegments(position=segments,
                                   color=colors, linewidth=size/2.))


def get_tf_from_point_normal(point, normal):
    tf = np.eye(4)
    # From https://math.stackexchange.com/questions/1956699/getting-a-transformation-matrix-from-a-normal-vector
    nx, ny, nz = normal
    if (nx**2 + ny**2 != 0.):
        nxny = np.sqrt(nx**2 + ny**2)
        tf[0, 0] = ny / nxny
        tf[0, 1] = -nx / nxny
        tf[1, 0] = nx*nz / nxny
        tf[1, 1] = ny*nz / nxny
        tf[1, 2] = -nxny
        tf[2, :3] = normal
    tf[:3, :3] = tf[:3, :3].T
    tf[0:3, 3] = np.array(point)
    return tf


def draw_plane_in_meshcat(vis, point, normal, size, name):
    size = np.array(size)
    vis["perception"]["tabletopsegmenter"][name].set_object(
        meshcat_g.Box(size))

    box_tf = get_tf_from_point_normal(point, normal)
    vis["perception"]["tabletopsegmenter"][name].set_transform(box_tf)


class TabletopObjectSegmenter:
    def __init__(self,
                 zmq_url="tcp://127.0.0.1:6000",
                 visualize=False):

        print("Opening meshcat vis... will hang if no server"
              "\tis running.")
        self.vis = None
        if visualize:
            self.vis = meshcat.Visualizer(zmq_url=zmq_url)
            self.vis["perception"].delete()
            self.vis["perception/tabletopsegmenter"].delete()

    def get_table_surface_plane_info(self, polyData):
        major_planes, plane_infos = \
            getMajorPlanes(polyData, useVoxelGrid=False,
                           distanceToPlaneThreshold=0.005)
        coloriter = iter(plt.cm.rainbow(np.linspace(0, 1, len(major_planes))))
        scores = []
        means = []
        stds = []
        for i, plane in enumerate(major_planes):
            # Calculate this plane score, by checking is total size,
            # closeness to the camera (i.e. lowest z, since camera is at
            # origin looking up), and centrality in image
            # (i.e. closest to z axis)
            centrality_weight = 2.
            closeness_weight = 10.
            size_weight = 1.
            # Calculate mean + std in R^3
            pts = vtkNumpy.getNumpyFromVtk(plane)
            mean = np.mean(pts, axis=0)
            std = np.std(pts, axis=0)
            means.append(mean)
            stds.append(std)
            score = centrality_weight * np.linalg.norm(mean[0:2]) + \
                closeness_weight * mean[2] + \
                size_weight * 1. / max(std)
            score /= (centrality_weight + closeness_weight + size_weight)
            scores.append(score)
        scores = np.array(scores)
        scores = (scores - np.min(scores)) / (np.max(scores) - np.min(scores))

        if self.vis is not None:
            for i, plane in enumerate(major_planes):
                score_color = plt.cm.RdYlGn(1. - scores[i])
                draw_polydata_in_meshcat(self.vis, plane, "planes/%02d" % i,
                                         score_color, size=0.01)

        best_plane_i = np.argmin(scores)
        pt = plane_infos[best_plane_i][0]
        normal = plane_infos[best_plane_i][1]
        if np.dot(normal, [0., 0., 1.]) > 0.:
            normal *= -1.
        # Change pt to be at the table center
        pt = means[best_plane_i] - (means[best_plane_i] - pt).dot(normal)
        return pt, normal

    def segment_pointcloud(self, polyData, plane_info=None):
        polyDataSimplified = applyVoxelGrid(polyData, leafSize=0.005)
        if self.vis is not None:
            draw_polydata_in_meshcat(self.vis, polyDataSimplified,
                                     "simplified_input")
        if plane_info is None:
            pt, normal = self.get_table_surface_plane_info(polyDataSimplified)
        else:
            pt, normal = plane_info
        # Todo: figure out which points are "on top of" that table cluster
        points = vtkNumpy.getNumpyFromVtk(polyData).T
        height_above_surface = np.dot(points.T - pt, normal)
        vtkNumpy.addNumpyToVtk(polyData, height_above_surface,
                               'height_above_surface')
        tabletopPoints = filterUtils.thresholdPoints(
            polyData, 'height_above_surface', [0.005, 0.05])

        distance_from_table_center = np.linalg.norm(
            vtkNumpy.getNumpyFromVtk(tabletopPoints) - pt, axis=1)
        vtkNumpy.addNumpyToVtk(tabletopPoints, distance_from_table_center,
                               'distance_from_table_center')
        tabletopPoints = filterUtils.thresholdPoints(
            tabletopPoints, 'distance_from_table_center', [0., 0.1])

        # tabletopPoints = applyVoxelGrid(tabletopPoints, leafSize=0.001)
        # Finally, transform those tabletop points to point up
        tf = transformUtils.getTransformFromOriginAndNormal(
            pt, normal).GetLinearInverse()
        tabletopPoints = filterUtils.transformPolyData(tabletopPoints, tf)
        old_normals = vtkNumpy.getNumpyFromVtk(tabletopPoints, "Normals")
        vtkNumpy.addNumpyToVtk(
            tabletopPoints,
            transformUtils.getNumpyFromTransform(tf)[0:3, 0:3].dot(
                old_normals.T).T,
            "Normals")
        draw_polydata_in_meshcat(self.vis, tabletopPoints, "tabletopPoints",
                                 color=[0., 0., 1.], size=0.001,
                                 with_normals=True)

        return tabletopPoints, (pt, normal)

    def fuse_polydata_list_with_octomap(self, all_pds):
        cell_size = 0.002
        tree = octomap.OcTree(cell_size)
        tree.setOccupancyThres(0.9)
        print("Prob hit: %f, prob miss: %f" %
              (tree.getProbHit(), tree.getProbMiss()))
        print("Occupancy thresh: %f" % (tree.getOccupancyThres()))
        for pd in all_pds:
            tree.insertPointCloud(
                vtkNumpy.getNumpyFromVtk(pd).astype(np.float64),
                np.zeros(3))
        output_cloud = np.zeros((tree.size(), 3))
        output_color = np.zeros((tree.size(), 4))
        k = 0
        for node in tree.begin_tree():
            if node.isLeaf() and tree.isNodeOccupied(node):
                output_cloud[k, :] = np.array(node.getCoordinate())
                output_color[k, :] = plt.cm.rainbow(node.getOccupancy())
                k += 1
        output_cloud.resize((k, 3))
        output_color.resize((k, 4))

        if self.vis is not None:
            self.vis["perception"]["tabletopsegmenter"]["fused_cloud"]\
                .set_object(meshcat_g.PointCloud(position=output_cloud.T,
                                                 color=output_color.T,
                                                 size=cell_size))
        return vtkNumpy.numpyToPolyData(output_cloud)

    def find_flippable_planes(self, polyData):
        major_planes, plane_infos = \
            getMajorPlanes(polyData, useVoxelGrid=False,
                           distanceToPlaneThreshold=0.001)
        coloriter = iter(plt.cm.rainbow(np.linspace(0, 1, len(major_planes))))
        scores = []
        means = []
        stds = []
        for i, plane in enumerate(major_planes):
            # Calculate this plane score, by checking is total size,
            # closeness to the camera (i.e. lowest z, since camera is at
            # origin looking up), and centrality in image
            # (i.e. closest to z axis)
            centrality_weight = 2.
            closeness_weight = 10.
            size_weight = 1.
            # Calculate mean + std in R^3
            pts = vtkNumpy.getNumpyFromVtk(plane)
            mean = np.mean(pts, axis=0)
            std = np.std(pts, axis=0)
            means.append(mean)
            stds.append(std)
            score = centrality_weight * np.linalg.norm(mean[0:2]) + \
                closeness_weight * mean[2] + \
                size_weight * 1. / max(std)
            score /= (centrality_weight + closeness_weight + size_weight)
            scores.append(score)
        scores = np.array(scores)
        scores = (scores - np.min(scores)) / (np.max(scores) - np.min(scores))

        if self.vis is not None:
            for i, plane in enumerate(major_planes):
                score_color = plt.cm.RdYlGn(1. - scores[i])
                draw_polydata_in_meshcat(self.vis, plane, "flippable/%02d" % i,
                                         score_color, size=0.001)
        best_plane_i = np.argmin(scores)
        pt = plane_infos[best_plane_i][0]
        normal = plane_infos[best_plane_i][1]
        if np.dot(normal, [0., 0., 1.]) > 0.:
            normal *= -1.
        # Change pt to be at the table center
        pt = means[best_plane_i] - (means[best_plane_i] - pt).dot(normal)
        return pt, normal

if __name__ == "__main__":
    np.set_printoptions(precision=5, suppress=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--scans_dir",
                        type=str,
                        help="Point cloud file to segment.",
                        default="/home/gizatt/spartan/data_volume/"
                                "carrot_scans")
    parser.add_argument("--topic",
                        type=str,
                        help="Point cloud topic.",
                        default="/camera/depth_registered/points")
    parser.add_argument("--ignore_live",
                        help="Whether to use the point cloud file.",
                        default=False, action='store_true')
    args = parser.parse_args()

    if args.ignore_live is True:
        scans_dir = "/home/gizatt/spartan/data_volume/carrot_scans/"
        scans = []
        for (dir, _, files) in os.walk(scans_dir):
            for f in files:
                path = os.path.join(dir, f)
                if os.path.exists(path) and \
                   path.split("/")[-1] == "fusion_pointcloud.ply":
                    scans.append(path)

        for scan in scans:
            print("Trying scan %s" % scan)
            polyData = ioUtils.readPolyData(scan)
            segmenter = TabletopObjectSegmenter(visualize=True)
            segmenter.segment_pointcloud(polyData)
            raw_input("Enter to continue...")
    else:
        rospy.init_node("tabletop_segmenter")
        sub = rosUtils.SimpleSubscriber(
            topic=args.topic, messageType=sensor_msgs.msg.PointCloud2)
        sub.start(queue_size=1)
        plt.figure()
        segmenter = TabletopObjectSegmenter(visualize=True)
        while (1):
            print("Waiting for pc on channel %s..." % args.topic, end="")
            all_pds = []
            plane_info = None
            for k in range(1):
                pc2 = sub.waitForNextMessage()
                print("Processing scan...")
                points, normals = do_pointcloud_preprocessing(pc2)
                polyData = convert_pc_np_to_vtk(points, normals)
                pd, plane_info = segmenter.segment_pointcloud(
                    polyData, plane_info=plane_info)
                all_pds.append(pd)
            #fused_polydata = segmenter.fuse_polydata_list_with_octomap(all_pds)
            #candidate_flip_surfaces = segmenter.find_flippable_planes(
            #    fused_polydata)
            #np.save("cloud.npy", vtkNumpy.getNumpyFromVtk(pd))
            #raw_input("Etner to continue...")
