def turn_right():
    turn_left()
    turn_left()
    turn_left()


def jump():
    turn_right()
    move()
    turn_right()


while not at_goal():
    if front_is_clear() == True:
        move()
    if right_is_clear() == True:
        jump()
    if wall_in_front() == True:
        turn_left()















