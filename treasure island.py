print('''
*******************************************************************************
          |                   |                  |                     |
 _________|________________.=""_;=.______________|_____________________|_______
|                   |  ,-"_,=""     `"=.|                  |
|___________________|__"=._o`"-._        `"=.______________|___________________
          |                `"=._o`"=._      _`"=._                     |
 _________|_____________________:=._o "=._."_.-="'"=.__________________|_______
|                   |    __.--" , ; `"=._o." ,-"""-._ ".   |
|___________________|_._"  ,. .` ` `` ,  `"-._"-._   ". '__|___________________
          |           |o`"=._` , "` `; .". ,  "-._"-._; ;              |
 _________|___________| ;`-.o`"=._; ." ` '`."\` . "-._ /_______________|_______
|                   | |o;    `"-.o`"=._``  '` " ,__.--o;   |
|___________________|_| ;     (#) `-.o `"=.`_.--"_o.-; ;___|___________________
____/______/______/___|o;._    "      `".o|o_.--"    ;o;____/______/______/____
/______/______/______/_"=._o--._        ; | ;        ; ;/______/______/______/_
____/______/______/______/__"=._o--._   ;o|o;     _._;o;____/______/______/____
/______/______/______/______/____"=._o._; | ;_.--"o.--"_/______/______/______/_
____/______/______/______/______/_____"=.o|o_.--""___/______/______/______/____
/______/______/______/______/______/______/______/______/______/______/_____ /
*******************************************************************************
''')
print("Welcome to Treasure Island.")
print("Your mission is to find the treasure.")
first_step = input("You have entered an island. Now, you need to choose which direction you want to head to. Please type left or right\n").lower()

if first_step == "right" or first_step != "left":
    print("I am sorry - your journey has ended in the hole with snakes. Try again")
elif first_step == "left":
    second_step = input("You have traveled to see a lake with a sign saying: 'One shall pass upon the night water drying or by the courage of the sun' - please type swim or wait\n").lower()
    if second_step == "swim" or second_step != "wait":
        print("I am sorry - you have drawned. One should only swim during the night.")
    elif second_step == "wait":
        third_step = input("You've passed the moonlight lake and now you see 3 doors in front of you - please type select red, yellow of blue\n").lower()
        if third_step == "yellow":
            print("Congratulations! You have reached the treasures and now you are Richie Rich!")
        else:
            print("Long path - yet no treasures. Good luck next time!")
