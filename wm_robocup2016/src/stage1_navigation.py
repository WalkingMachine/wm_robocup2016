#!/usr/bin/env python

import rospy
import smach
from smach_ros import SimpleActionState, IntrospectionServer
from actionlib import SimpleActionClient
from actionlib_msgs.msg import GoalStatus
import wm_supervisor.srv
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from nav_msgs.srv import GetPlan
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from std_msgs.msg import String, Float64, UInt8, Bool
from math import sqrt, atan2
import tf_conversions
from tf2_ros import Buffer, TransformListener
from tf2_geometry_msgs import do_transform_pose
import threading
from open_door_detector.srv import detect_open_door, detect_open_doorRequest
import actionlib
from face_detector.msg import FaceDetectorAction, FaceDetectorGoal
from wm_people_follower.srv import peopleFollower, peopleFollowerRequest, peopleFollowerResponse

GREEN_FACE = 3
YELLOW_FACE = 4
RED_FACE = 5

ARENA_A = True


class InitState(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['init_done'])
        self.neck_pub = rospy.Publisher('neckHead_controller/command', Float64, queue_size=1, latch=True)
        self.amcl_initial_pose_pub = rospy.Publisher('initialpose', PoseWithCovarianceStamped, queue_size=1, latch=True)
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)

    def execute(self, ud):
        rospy.logdebug("Entered 'INIT_STATE' state.")

        self.face_cmd.publish(GREEN_FACE)

        initial_pose = PoseWithCovarianceStamped()
        initial_pose.header.frame_id = 'map'

        if not ARENA_A:
            initial_pose.pose.pose.position.x = 15.322
            initial_pose.pose.pose.position.y = 5.537
            initial_pose.pose.pose.orientation.x = 0.0
            initial_pose.pose.pose.orientation.y = 0.0
            initial_pose.pose.pose.orientation.z = 0.693
            initial_pose.pose.pose.orientation.w = 0.721
        else:
            initial_pose.pose.pose.position.x = 0.0
            initial_pose.pose.pose.position.y = 0.0
            initial_pose.pose.pose.orientation.x = 0.0
            initial_pose.pose.pose.orientation.y = 0.0
            initial_pose.pose.pose.orientation.z = 0.0
            initial_pose.pose.pose.orientation.w = 1.0

        self.amcl_initial_pose_pub.publish(initial_pose)

        neck_cmd = Float64()
        neck_cmd.data = -2.0
        self.neck_pub.publish(neck_cmd)

        rospy.sleep(rospy.Duration(2))
        neck_cmd.data = -0.7
        self.neck_pub.publish(neck_cmd)

        tts_msg = String()
        tts_msg.data = "I am ready to begin the navigation test."
        self.tts_pub.publish(tts_msg)

        return 'init_done'


class WaitDoor(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['wait_timed_out', 'door_is_open', 'door_is_closed'])
        self.door_detector_srv = rospy.ServiceProxy('/detect_open_door', detect_open_door)
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)
        self.iter = 0

    def execute(self, ud):
        rospy.logdebug("Entered 'WAIT_FOR_OPEN_DOOR' state.")

        try:
            req = detect_open_doorRequest()
            req.aperture_angle = 0.60
            req.wall_distance = 1.0
            req.min_door_width = 0.4
            res = self.door_detector_srv(req)

            if res.door_pos.pose.position.x != 0.0 or res.door_pos.pose.position.y != 0.0:
                return 'door_is_open'

        except rospy.ServiceException:
            rospy.logerr("Open door service call failed")
            pass

        self.iter += 1
        if self.iter < 5:
            rospy.sleep(1.0)
            return 'door_is_closed'
        else:
            self.face_cmd.publish(YELLOW_FACE)
            tts_msg = String()
            tts_msg.data = "I did not detect an opened door in the allocated time."
            self.tts_pub.publish(tts_msg)
            tts_msg.data = "I require that you press the start button."
            return 'wait_timed_out'


class StartOverride(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['start_signal_received'])
        self.start_signal_sub = rospy.Subscriber('start_button_msg', Bool, self.start_signal_sub, queue_size=1)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)
        self.signal_received = False

        self.mutex = threading.Lock()

    def start_signal_sub(self, signal):

        self.mutex.acquire()

        if signal.data:
            self.signal_received = True

        self.mutex.release()

        return

    def execute(self, ud):
        rospy.logdebug("Entered 'START_OVERRIDE' state.")

        while True:
            self.mutex.acquire()
            if self.signal_received:
                self.mutex.release()
                break
            self.mutex.release()
            rospy.sleep(rospy.Duration(1))

        self.face_cmd.publish(GREEN_FACE)
        rospy.sleep(rospy.Duration(10))
        return 'start_signal_received'


class AnnounceAction(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['announcement_done'], input_keys=['aa_target_wp', 'aa_wp_str'])

        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)

    def execute(self, ud):
        rospy.logdebug("Entered 'ANNOUNCE_ACTION' state.")
        tts_msg = String()
        tts_msg.data = "I am moving toward " + ud.aa_wp_str[ud.aa_target_wp - 1] + "."
        self.tts_pub.publish(tts_msg)
        return 'announcement_done'


class InitSupervisor(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['status_ok', 'status_estop'])
        self.status_service = rospy.ServiceProxy('robot_status', wm_supervisor.srv.robotStatus)

    def execute(self, ud):
        rospy.logdebug("Entered 'ROBOT_STATUS' state.")

        try:
            res = self.status_service()

        except rospy.ServiceException:
            rospy.logfatal("Could not get the robot's status. Aborting...")
            return 'status_estop'

        if res.status == wm_supervisor.srv.robotStatusResponse.STATUS_OK:
            return 'status_ok'

        rospy.sleep(5.0)

        return 'status_estop'


class InitMove(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['succeeded', 'supervise'])
        self.move_base_client = SimpleActionClient('move_base', MoveBaseAction)

    def execute(self, ud):

        self.move_base_client.wait_for_server()

        goal = MoveBaseGoal()
        pose = PoseStamped()
        pose.header.frame_id = 'map'
        pose.header.stamp = rospy.Time.now()
        pose.pose.position.x = 2.0
        pose.pose.position.y = 0.0
        pose.pose.orientation.x = 0.0
        pose.pose.orientation.y = 0.0
        pose.pose.orientation.z = 0.0
        pose.pose.orientation.w = 1.0

        goal.target_pose = pose
        goal.target_pose.header.stamp = rospy.Time.now()

        self.move_base_client.send_goal(goal)
        self.move_base_client.wait_for_result(rospy.Duration(30))

        status = self.move_base_client.get_state()
        if status == GoalStatus.SUCCEEDED:
            return 'succeeded'
        else:
            return 'supervise'


class RobotStatus(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['status_ok', 'status_error', 'status_estop'])
        self.status_service = rospy.ServiceProxy('robot_status', wm_supervisor.srv.robotStatus)

    def execute(self, ud):
        rospy.logdebug("Entered 'ROBOT_STATUS' state.")

        try:
            res = self.status_service()

        except rospy.ServiceException:
            rospy.logfatal("Could not get the robot's status. Aborting...")
            return 'status_estop'

        if res.status == wm_supervisor.srv.robotStatusResponse.STATUS_OK:
            return 'status_ok'

        rospy.sleep(5.0)

        return 'status_estop'


class Move(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['succeeded', 'aborted', 'preempted'],
                             input_keys=['move_waypoints', 'move_target_wp'])
        self.move_base_client = SimpleActionClient('move_base', MoveBaseAction)

    def execute(self, ud):

        self.move_base_client.wait_for_server()

        goal = MoveBaseGoal()
        goal.target_pose = ud.move_waypoints[ud.move_target_wp - 1]
        goal.target_pose.header.stamp = rospy.Time.now()

        self.move_base_client.send_goal(goal)
        self.move_base_client.wait_for_result(rospy.Duration(30))

        status = self.move_base_client.get_state()
        if status == GoalStatus.SUCCEEDED:
            return 'succeeded'
        elif status == GoalStatus.PREEMPTED:
            return 'preempted'
        else:
            return 'aborted'


class AttemptMonitor(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['monitoring_done', 'wp2_case', 'monitor_failed'],
                             input_keys=['ma_current_attempt', 'ma_attempt_limit', 'ma_target_wp', 'ma_waypoints',
                                         'ma_wp_str'],
                             output_keys=['ma_current_attempt', 'ma_target_wp'])

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer)

        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)

        self.grasp_distance = 1.6

    def execute(self, ud):
        rospy.logdebug("Entered 'MONITOR_ATTEMPTS' state.")

        # attempt limit not reached, try to reach target again
        if ud.ma_current_attempt < ud.ma_attempt_limit:

            if ud.ma_target_wp == 2:
                """
                # wp2 is a special case
                we know that wp2 is initially blocked by an obstacle and cannot be reached until the obstacle move or is moved
                we consider the robot has reached the target if it is within grasp distance
                """

                tf_stamped = self.tf_buffer.lookup_transform('map', 'base_link', rospy.Time(0))

                if sqrt((ud.ma_waypoints[1].pose.position.x - tf_stamped.transform.translation.x) ** 2 +
                        (ud.ma_waypoints[1].pose.position.y - tf_stamped.transform.translation.y) ** 2) < self.grasp_distance:
                    return 'wp2_case'
                else:
                    if ud.ma_current_attempt < ud.ma_attempt_limit:
                        ud.ma_current_attempt += 1

                    else:
                        ud.ma_current_attempt = 1
                        tts_msg = String()
                        tts_msg.data = "I can not reach" + ud.ma_wp_str[ud.ma_target_wp - 1] + "." + \
                                       "I am moving toward the next waypoint."
                        self.tts_pub.publish(tts_msg)
                        ud.ma_target_wp += 1

                    return 'monitoring_done'
            else:

                ud.ma_current_attempt += 1
                return 'monitoring_done'
        else:
            # attempt limit reached, skip to the next goal
            ud.ma_current_attempt = 1
            self.face_cmd.publish(YELLOW_FACE)
            tts_msg = String()
            tts_msg.data = "I can not reach" + ud.ma_wp_str[ud.ma_target_wp - 1] + "." + "I am moving toward the next waypoint."
            self.tts_pub.publish(tts_msg)
            ud.ma_target_wp += 1
            return 'monitoring_done'


class ScanFace(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['face_scan_done'])

        self.face_detector_ac = actionlib.SimpleActionClient('face_positions', FaceDetectorAction)
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.neck_pub = rospy.Publisher('neckHead_controller/command', Float64, queue_size=1, latch=True)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)

    def execute(self, ud):
        rospy.logdebug("Entered 'SCAN_FACE' state.")

        neck_cmd = Float64()
        neck_cmd.data = 0.0
        self.neck_pub.publish(neck_cmd)

        rospy.sleep(rospy.Duration(2))

        tts_msg = String()

        if self.face_detector_ac.wait_for_server(rospy.Duration(10)):  # wait for action server
            self.face_detector_ac.send_goal(FaceDetectorGoal())  # send goal

            if self.face_detector_ac.wait_for_result(rospy.Duration(10)):
                res = self.face_detector_ac.get_result()

                # verify that action result is not empty
                if len(res.face_positions) > 0:
                    human_in_range = False
                    # check that detected faces are not too far from the robot
                    for f in range(len(res.face_positions)):
                        distance_to_human = sqrt(res.face_positions[f].pos.x**2 +
                                                 res.face_positions[f].pos.y**2)
                        if distance_to_human < 2.0:
                            human_in_range = True
                            break

                    if human_in_range:
                        tts_msg.data = "A human is blocking the path to the waypoint."
                        self.tts_pub.publish(tts_msg)
                        tts_msg.data = "Excuse me, I need you to move so I can reach my target waypoint."
                        self.tts_pub.publish(tts_msg)

                # action result is empty, assume the path if not blocked by a human
                else:
                    tts_msg.data = "The path is blocked by an unknown object."
                    self.tts_pub.publish(tts_msg)
            # if action times out, assume no faces were detected and the path is not blocked by a human
            else:
                tts_msg.data = "The obstacle blocking the path is not a human."
                self.tts_pub.publish(tts_msg)

        # wait for server timed out. moving on...
        else:
            self.face_cmd.publish(YELLOW_FACE)
            tts_msg.data = "I am unable to identify the obstacle."
            self.tts_pub.publish(tts_msg)

        return 'face_scan_done'


class WaitForObstacle(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['wait_over'],
                             input_keys=['wait_target_wp', 'wait_waypoints'],
                             output_keys=['wait_target_wp'])
        self.move_base_srv = rospy.ServiceProxy('/move_base/make_plan', GetPlan)
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer)

    def execute(self, ud):
        rospy.logdebug("Entered 'WAIT_FOR_OBSTACLE' state.")

        nb_loop = 0
        max_nb_loop = 5

        start_pose = PoseStamped()
        tf_stamped = self.tf_buffer.lookup_transform('map', 'base_link', rospy.Time(0))
        start_pose = do_transform_pose(start_pose, tf_stamped)

        goal_pose = ud.wait_waypoints[1]  # wp2

        goal_tolerance = 0.10

        while nb_loop < max_nb_loop:
            try:
                res = self.move_base_srv(start=start_pose, goal=goal_pose, tolerance=goal_tolerance)
                if res.plan.poses:
                    break
            except rospy.ServiceException:
                pass

            nb_loop += 1
            rospy.sleep(rospy.Duration(5))

        if nb_loop > max_nb_loop:
            ud.wait_target_wp += 1
            tts_msg = String()
            tts_msg.data = "I detect that the path to the waypoint is still obstructed."
            self.tts_pub.publish(tts_msg)
            tts_msg.data = "I have waited long enough. I am moving toward the next waypoint."
            self.tts_pub.publish(tts_msg)

        return 'wait_over'


class AnnounceWpReached(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['general_case', 'wp3_case'],
                             input_keys=['aw_target_wp', 'aw_wp_str'],
                             output_keys=['aw_target_wp'])
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)

    def execute(self, ud):
        rospy.logdebug("Entered 'ANNOUNCE_WP_REACHED' state.")

        self.face_cmd.publish(GREEN_FACE)

        tts_msg = String()

        tts_msg.data = "I have reached " + ud.aw_wp_str[ud.aw_target_wp - 1] + "."

        rospy.sleep(rospy.Duration(2))

        if ud.aw_target_wp == 3:
            return 'wp3_case'
        else:
            tts_msg.data = "I a moving on to the next waypoint."
            self.tts_pub.publish(tts_msg)
            ud.aw_target_wp += 1
            return 'general_case'


class TellInstructions(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['target_locked', 'target_not_locked'])

        # TODO
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.people_follower_srv = rospy.ServiceProxy('wm_people_follow', peopleFollower)

    def execute(self, ud):
        rospy.logdebug("Entered 'TELL_INSTRUCTIONS' state.")

        # TODO

        tts_msg = String()
        tts_msg.data = "Hello, my name is SARA. I will follow you to the next waypoint once I am ready."
        self.tts_pub.publish(tts_msg)
        tts_msg.data = "Please stand still, approximately 1 meter in front of me, facing me, while I memorize your features."
        self.tts_pub.publish(tts_msg)
        rospy.sleep(5.0)

        loop_again = True

        while loop_again:
            try:
                res = self.people_follower_srv.call(request=peopleFollowerRequest.ACQUIRE_TARGET)
                if res.response == peopleFollowerResponse.SUCCESS:
                    loop_again = False
                else:
                    tts_msg.data = "I was not able to get your features. Please stand still, approximately 1 meter in front of me, facing me."
                    self.tts_pub.publish(tts_msg)

            except rospy.ServiceException:
                rospy.sleep(rospy.Duration(8))
                pass

        tts_msg.data = "When you want me to stop following you, say 'SARA go back home'."
        self.tts_pub.publish(tts_msg)
        tts_msg.data = "You must start your instructions by calling my name. Otherwise, I will ignore your instructions."
        self.tts_pub.publish(tts_msg)
        rospy.sleep(rospy.Duration(8))
        tts_msg.data = "I am now ready to follow you."
        self.tts_pub.publish(tts_msg)

        try:
            self.people_follower_srv.call(request=peopleFollowerRequest.START_FOLLOWING)
        except rospy.ServiceException:
            pass

        return 'target_locked'


class MonitorFollowing(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['stop_following'])
        self.audio_input = rospy.Subscriber('recognizer1/output', String, self.audio_cb)
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.people_follower_srv = rospy.ServiceProxy('wm_people_follow', peopleFollower)

        self.mutex = threading.Lock()
        self.stop_following = False

    def audio_cb(self, msg):

        self.mutex.acquire()

        if msg.data.lower().find('sara') != -1 or msg.data.lower().find('sarah') != -1:
            if msg.data.lower().find('home') != -1:
                self.stop_following = True

        self.mutex.release()

        return

    def execute(self, ud):
        rospy.logdebug("Entered 'MONITOR_FOLLOWING' state.")

        while True:
            self.mutex.acquire()

            if self.stop_following:
                self.people_follower_srv.call(request=peopleFollowerRequest.STOP_FOLLOWING)
                self.mutex.release()
                break

            self.mutex.release()
            rospy.sleep(rospy.Duration(1))

        tts_msg = String()
        tts_msg.data = "I will stop following you and go back home."
        self.tts_pub.publish(tts_msg)

        return 'stop_following'


class GoBackSupervisor(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['go_back_estop', 'go_back_ok', 'go_back_error'])
        self.status_service = rospy.ServiceProxy('robot_status', wm_supervisor.srv.robotStatus)

    def execute(self, ud):
        rospy.logdebug("Entered 'GO_BACK_SUPERVISOR' state.")

        try:
            res = self.status_service()

        except rospy.ServiceException:
            rospy.logfatal("Could not get the robot status. Aborting...")
            return 'go_back_error'

        if res.status == wm_supervisor.srv.robotStatusResponse.STATUS_OK:
            return 'go_back_ok'

        rospy.sleep(5.0)

        return 'go_back_estop'


class FailTest(smach.State):
    def __init__(self):
        smach.State.__init__(self, outcomes=['exit'])
        # TODO
        # connect to service to turn face red, announce the robot cannot recover autonomously
        self.tts_pub = rospy.Publisher('sara_tts', String, queue_size=1, latch=True)
        self.face_cmd = rospy.Publisher('/face_mode', UInt8, queue_size=1, latch=True)

    def execute(self, ud):
        # TODO
        self.face_cmd.publish(RED_FACE)

        tts_msg = String
        tts_msg.data = "I can't continue the test. I have encountered an error from which I can't recover."
        self.tts_pub.publish(tts_msg)

        return 'exit'


if __name__ == '__main__':

    rospy.init_node('stage1_navigation_node')
    sm = smach.StateMachine(outcomes=['test_failed', 'test_succeeded'])

    if ARENA_A:

        wp1 = PoseStamped()
        wp1.header.frame_id = 'map'
        wp1.pose.position.x = 3.6831
        wp1.pose.position.y = -7.25051
        wp1.pose.position.z = 0.0
        wp1.pose.orientation.x = 0.0
        wp1.pose.orientation.y = 0.0
        wp1.pose.orientation.z = 0.696826
        wp1.pose.orientation.w = 0.71724

        wp2 = PoseStamped()
        wp2.header.frame_id = 'map'
        wp2.pose.position.x = 7.01055
        wp2.pose.position.y = -7.66038
        wp2.pose.position.z = 0.0
        wp2.pose.orientation.x = 0.0
        wp2.pose.orientation.y = 0.0
        wp2.pose.orientation.z = -0.307491
        wp2.pose.orientation.w = 0.951551

        wp3 = PoseStamped()
        wp3.header.frame_id = 'map'
        wp3.pose.position.x = 5.83367
        wp3.pose.position.y = -10.265
        wp3.pose.position.z = 0.0
        wp3.pose.orientation.x = 0.0
        wp3.pose.orientation.y = 0.0
        wp3.pose.orientation.z = -0.721783
        wp3.pose.orientation.w = 0.69212

        wp4 = PoseStamped()
        wp4.header.frame_id = 'map'
        wp4.pose.position.x = 5.83367
        wp4.pose.position.y = -10.265
        wp4.pose.position.z = 0.0
        wp4.pose.orientation.x = 0.0
        wp4.pose.orientation.y = 0.0
        wp4.pose.orientation.z = -0.721783
        wp4.pose.orientation.w = 0.69212

    else:
        wp1 = PoseStamped()
        wp1.header.frame_id = 'map'
        wp1.pose.position.x = 10.148
        wp1.pose.position.y = -1.117
        wp1.pose.position.z = 0.0
        wp1.pose.orientation.x = 0.0
        wp1.pose.orientation.y = 0.0
        wp1.pose.orientation.z = 1.0
        wp1.pose.orientation.w = 0.0

        wp2 = PoseStamped()
        wp2.header.frame_id = 'map'
        wp2.pose.position.x = 13.899
        wp2.pose.position.y = -1.083
        wp2.pose.position.z = 0.0
        wp2.pose.orientation.x = 0.0
        wp2.pose.orientation.y = 0.0
        wp2.pose.orientation.z = 0.728
        wp2.pose.orientation.w = 0.685

        wp3 = PoseStamped()
        wp3.header.frame_id = 'map'
        wp3.pose.position.x = 2.545
        wp3.pose.position.y = 1.487
        wp3.pose.position.z = 0.0
        wp3.pose.orientation.x = 0.0
        wp3.pose.orientation.y = 0.0
        wp3.pose.orientation.z = 1.0
        wp3.pose.orientation.w = 0.0

        wp4 = PoseStamped()
        wp4.header.frame_id = 'map'
        wp4.pose.position.x = 2.545
        wp4.pose.position.y = 1.487
        wp4.pose.position.z = 0.0
        wp4.pose.orientation.x = 0.0
        wp4.pose.orientation.y = 0.0
        wp4.pose.orientation.z = 1.0
        wp4.pose.orientation.w = 0.0

    sm.userdata.target_wp = 1
    sm.userdata.waypoints = [wp1, wp2, wp3, wp4]
    sm.userdata.wp_str = ["waypoint one", "waypoint two", "waypoint three", "waypoint four"]

    sm.userdata.attempt_limit = 3
    sm.userdata.current_attempt = 1

    with sm:

        def move_base_goal_cb(userdata, default_goal):
            rospy.logdebug("Entered move_base goal callback.")
            move_goal = MoveBaseGoal()
            move_goal.target_pose = userdata.mb_cb_waypoints[userdata.mb_cb_target_wp - 1]
            move_goal.target_pose.header.stamp = rospy.Time.now()
            # print move_goal.target_pose
            return move_goal


        def align_cb(userdata, default_goal):
            rospy.logdebug("Entered align base callback.")
            align_goal = MoveBaseGoal()
            tf_buffer = Buffer()
            tf_listener = TransformListener(tf_buffer)

            tf_stamped = tf_buffer.lookup_transform('map', 'base_link', rospy.Time(0), rospy.Duration(10))

            # we don't want the robot to translate, only to rotate
            align_goal.target_pose.pose.position.x = tf_stamped.transform.translation.x
            align_goal.target_pose.pose.position.y = tf_stamped.transform.translation.y

            # get orientation to face waypoint 2
            desired_yaw = atan2(userdata.al_cb_waypoints[1].pose.position.y - align_goal.target_pose.pose.position.y,
                                userdata.al_cb_waypoints[1].pose.position.x - align_goal.target_pose.pose.position.x)
            q = tf_conversions.transformations.quaternion_from_euler(0.0, 0.0, desired_yaw)
            align_goal.target_pose.pose.orientation.x = q[0]
            align_goal.target_pose.pose.orientation.y = q[1]
            align_goal.target_pose.pose.orientation.z = q[2]
            align_goal.target_pose.pose.orientation.w = q[3]

            align_goal.target_pose.header.frame_id = 'map'
            align_goal.target_pose.header.stamp = rospy.Time.now()

            return align_goal

        def go_back_home_cb(userdata, default_goal):
            rospy.logdebug("Entered go back home callback.")
            go_back_goal = MoveBaseGoal()
            go_back_goal.target_pose = userdata.gb_cb_waypoints[2]  # waypoint 3
            go_back_goal.target_pose.header.frame_id = 'map'
            go_back_goal.target_pose.header.stamp = rospy.Time.now()
            return go_back_goal


        smach.StateMachine.add('INIT_STATE',
                               InitState(),
                               transitions={'init_done': 'START_OVERRIDE'})

        smach.StateMachine.add('START_OVERRIDE',
                               StartOverride(),
                               transitions={'start_signal_received': 'ANNOUNCE_ACTION'})
        """
        smach.StateMachine.add('WAIT_FOR_OPEN_DOOR',
                               WaitDoor(),
                               transitions={'door_is_open': 'ANNOUNCE_ACTION',
                                            'door_is_closed': 'WAIT_FOR_OPEN_DOOR'})
        """

        smach.StateMachine.add('ANNOUNCE_ACTION',
                               AnnounceAction(),
                               transitions={'announcement_done': 'INIT_SUPERVISOR'},
                               remapping={'aa_target_wp': 'target_wp',
                                          'aa_wp_str': 'wp_str'})

        smach.StateMachine.add('INIT_SUPERVISOR',
                               InitSupervisor(),
                               transitions={'status_ok': 'INIT_MOVE',
                                            'status_estop': 'INIT_SUPERVISOR'})

        smach.StateMachine.add('INIT_MOVE',
                               InitMove(),
                               transitions={'succeeded': 'ROBOT_STATUS',
                                            'supervise': 'INIT_SUPERVISOR'})

        smach.StateMachine.add('ROBOT_STATUS',
                               RobotStatus(),
                               transitions={'status_ok': 'MOVE',
                                            'status_error': 'TEST_FAILED',
                                            'status_estop': 'ROBOT_STATUS'})

        """
        smach.StateMachine.add('MOVE',
                               SimpleActionState('move_base',
                                                 MoveBaseAction,
                                                 goal_cb=move_base_goal_cb,
                                                 input_keys=['mb_cb_waypoints', 'mb_cb_target_wp']),
                               transitions={'succeeded': 'ANNOUNCE_WP_REACHED',
                                            'preempted': 'MONITOR_ATTEMPTS',
                                            'aborted': 'MONITOR_ATTEMPTS'},
                               remapping={'mb_cb_waypoints': 'waypoints',
                                          'mb_cb_target_wp': 'target_wp'})
        """

        smach.StateMachine.add('MOVE',
                               Move(),
                               transitions={'succeeded': 'ANNOUNCE_WP_REACHED',
                                            'preempted': 'MONITOR_ATTEMPTS',
                                            'aborted': 'MONITOR_ATTEMPTS'},
                               remapping={'move_waypoints': 'waypoints',
                                          'move_target_wp': 'target_wp'})

        smach.StateMachine.add('MONITOR_ATTEMPTS',
                               AttemptMonitor(),
                               transitions={'monitoring_done': 'ROBOT_STATUS',
                                            'wp2_case': 'ALIGN',
                                            'monitor_failed': 'MONITOR_ATTEMPTS'},
                               remapping={'ma_current_attempt': 'current_attempt',
                                          'ma_attempt_limit': 'attempt_limit',
                                          'ma_target_wp': 'target_wp',
                                          'ma_waypoints': 'waypoints',
                                          'ma_wp_str': 'wp_str'})

        smach.StateMachine.add('ALIGN',
                               SimpleActionState('move_base',
                                                 MoveBaseAction,
                                                 goal_cb=align_cb,
                                                 input_keys=['al_cb_target_wp']),
                               transitions={'succeeded': 'SCAN_FACE',
                                            'preempted': 'ALIGN',
                                            'aborted': 'ALIGN'},
                               remapping={'al_cb_waypoints': 'waypoints'})

        smach.StateMachine.add('SCAN_FACE',
                               ScanFace(),
                               transitions={'face_scan_done': 'WAIT_FOR_OBSTACLE'})

        smach.StateMachine.add('WAIT_FOR_OBSTACLE',
                               WaitForObstacle(),
                               transitions={'wait_over': 'ROBOT_STATUS'},
                               remapping={'wait_target_wp': 'target_wp',
                                          'wait_waypoints': 'waypoints'})

        smach.StateMachine.add('ANNOUNCE_WP_REACHED',
                               AnnounceWpReached(),
                               transitions={'general_case': 'ROBOT_STATUS',
                                            'wp3_case': 'TELL_FOLLOW_INSTRUCTIONS'},
                               remapping={'aw_target_wp': 'target_wp',
                                          'aw_wp_str': 'wp_str'})

        smach.StateMachine.add('TELL_FOLLOW_INSTRUCTIONS',
                               TellInstructions(),
                               transitions={'target_locked': 'MONITOR_FOLLOWING',
                                            'target_not_locked': 'TEST_FAILED'})

        smach.StateMachine.add('MONITOR_FOLLOWING',
                               MonitorFollowing(),
                               transitions={'stop_following': 'GO_BACK_SUPERVISOR'})

        smach.StateMachine.add('GO_BACK_SUPERVISOR',
                               GoBackSupervisor(),
                               transitions={'go_back_estop': 'GO_BACK_SUPERVISOR',
                                            'go_back_ok': 'GO_BACK_HOME',
                                            'go_back_error': 'TEST_FAILED'})

        smach.StateMachine.add('GO_BACK_HOME',
                               SimpleActionState('move_base',
                                                 MoveBaseAction,
                                                 goal_cb=go_back_home_cb,
                                                 input_keys=['gb_cb_waypoints']),
                               transitions={'succeeded': 'test_succeeded',
                                            'preempted': 'GO_BACK_SUPERVISOR',
                                            'aborted': 'GO_BACK_SUPERVISOR'},
                               remapping={'gb_cb_waypoints': 'waypoints'})

        smach.StateMachine.add('TEST_FAILED',
                               FailTest(),
                               transitions={'exit': 'test_failed'})

    sis = IntrospectionServer('smach_introspection_server', sm, 'navigation_smach')
    sis.start()

    outcome = sm.execute()

    while not rospy.is_shutdown():
        rospy.spin()

    sis.stop()
